import torch
import matplotlib.pyplot as plt
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import pandas as pd
import os
import io
from torch.utils.data import Dataset
from PIL import Image, ImageOps
import torchvision.models as models
import argparse
from scipy.ndimage.filters import gaussian_filter
import glob
import shutil
import wandb
import torchvision
import random
import torchvision.transforms.functional as TF
import segmentation_models_pytorch as smp

torch.set_printoptions(precision=4, linewidth=300)
np.set_printoptions(precision=4, linewidth=300)
pd.set_option('display.max_columns', None)


################################################ SET UP SEED AND ARGS AND EXIT HANDLER

def set_seed():
    np.random.seed(0)
    random.seed(0)
    torch.manual_seed(0)
    # torch.set_deterministic(True)
    torch.cuda.manual_seed(0)
    torch.cuda.manual_seed_all(0)
    torch.backends.cudnn.enabled = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"


set_seed()

parser = argparse.ArgumentParser()
parser.add_argument('--batch_size', type=int, default=1)
parser.add_argument('--epochs', type=int, default=1)
parser.add_argument('--lr', type=float, default=0.001)
parser.add_argument('--conv_thresh', type=float, default=0)
parser.add_argument('--save_freq', default=100, type=int)
parser.add_argument('--subset', default=100, type=int)
parser.add_argument('--train', default=False, action='store_true')
parser.add_argument('--load', default='')
parser.add_argument('--note', default='')
parser.add_argument('--model', default='smp.Unet')
parser.add_argument('--encoder', default='resnet34')
parser.add_argument('--cls_lf', default='nn.BCELoss()')
parser.add_argument('--seg_lf', default='f1_loss')
parser.add_argument('--cls', type=int, default=-1)
parser.add_argument('--slides', default=None)
parser.add_argument('--augment', default=False, action='store_true')
parser.add_argument('--classifier', default=False, action='store_true')
parser.add_argument('--downsample', default=False, action='store_true')
parser.add_argument('--dual', default=False, action='store_true')

args = parser.parse_args()

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# os.environ["WANDB_SILENT"] = "true"

if args.classifier:
    proj = "immune_cls"
else:
    proj = "immune_seg"

wandb.init(project=proj, entity="jessicamarycooper", config=args)
args = wandb.config
params_id = wandb.run.name
print(params_id, args)

dim = 256


################################################ HELPERS


def show_im(im):
    d = im.shape[-1]
    fig, ax = plt.subplots()
    im = im.reshape(-1, d)
    plt.imshow(im, cmap='gray')
    plt.show()
    plt.close(fig)


def save_im(im, name='image'):
    d = im.shape[-1]
    im = im.reshape(-1, d, d)

    for cls in range(im.shape[0]):
        fig, ax = plt.subplots()
        img = im[cls]
        plt.imshow(img, cmap='gray')
        plt.savefig('lc_imgs/' + name + '_' + str(cls) + '.png')
        plt.close(fig)


def standardise(img):
    return (img - img.mean()) / (img.std() + 0.0001)


def normalise(x):
    return (x - x.min()) / max(x.max() - x.min(), 0.0001)


def whiten(img):
    img = img - img.mean()
    cov = np.dot(img.T, img)
    d, vec = np.linalg.eigh(cov)
    diag = np.diag(1 / np.sqrt(d + 0.0001))
    w = np.dot(np.dot(vec, diag), vec.T)
    return np.dot(img, w)


def equalise(img):
    return np.sort(img.ravel()).searchsorted(img)


def get_mean_and_std(loader):
    mean = 0
    std = 0
    for i, data in enumerate(loader):
        inputs, target_masks, target_labels, orig_targets = data

        batch_samples = inputs.size(0)
        inputs = inputs.view(batch_samples, inputs.size(1), -1)
        mean += inputs.mean(2).sum(0)
        std += inputs.std(2).sum(0)

    mean /= len(loader.dataset)
    std /= len(loader.dataset)

    return mean, std


def min_pool(target, x, y):
    tb, tc, tx, ty = target.shape
    target = F.max_pool2d(target * -1, (tx // x, ty // y), (tx // x, ty // y)) * -1

    return target


################################################ METRICS


def scores(output, target):
    bce = F.binary_cross_entropy(output, target).item()

    output = torch.round(output)
    conf = output / target
    tp, fp, tn, fn = torch.sum(conf == 1).item(), torch.sum(conf == float('inf')).item(), torch.sum(torch.isnan(conf)).item(), torch.sum(conf == 0).item()

    if (tp + fp) == 0:
        precision = 0
    else:
        precision = tp / (tp + fp)

    if (tp + fn) == 0:
        recall = 0
    else:
        recall = tp / (tp + fn)

    f1 = 2 * ((precision * recall) / max(precision + recall, 0.0001))

    return np.array([bce, precision, recall, f1])


def f1_loss(output, target):
    tp = torch.sum(target * output)
    tn = torch.sum((1 - target) * (1 - output))
    fp = torch.sum((1 - target) * output)
    fn = torch.sum(target * (1 - output))

    p = tp / (tp + fp + 0.0001)
    r = tp / (tp + fn + 0.0001)

    f1 = 2 * p * r / (p + r + 0.0001)
    f1 = torch.where(torch.isnan(f1), torch.zeros_like(f1), f1)

    return 1 - torch.mean(f1)


def dice_loss(outputs, targets, smooth=1):
    outputs, targets = outputs.reshape(-1), targets.reshape(-1)

    intersection = (outputs * targets).sum()
    dice = (2. * intersection + smooth) / (outputs.sum() + targets.sum() + smooth)

    return 1 - dice


def iou_loss(outputs, targets, smooth=1):
    outputs, targets = outputs.reshape(-1), targets.reshape(-1)
    intersection = (outputs * targets).sum()
    total = (outputs + targets).sum()
    union = total - intersection

    iou = (intersection + smooth) / (union + smooth)

    return 1 - iou


def focal_loss(outputs, targets, alpha=0.8, gamma=2):
    outputs, targets = outputs.reshape(-1), targets.reshape(-1)
    # first compute binary cross-entropy
    BCE = F.binary_cross_entropy(outputs, targets, reduction='mean')
    BCE_EXP = torch.exp(-BCE)
    focal_loss = alpha * (1 - BCE_EXP) ** gamma * BCE

    return focal_loss


################################################ SETTING UP DATASET


if not os.path.exists('sample_paths.csv'):
    print('Building data paths...')

    data = glob.glob("data/new_exported_tiles/*/*")

    samples = {}
    for d in data:
        dn = d.split(']')[0] + ']'
        sample = samples[dn] if dn in samples else {}
        if '-labelled' in d:
            sample['Mask'] = d
            sample['Classes'] = np.unique(np.array(Image.open(d)))
        else:
            sample['Img'] = d
        sample['Slide'] = dn.split('/')[2]
        samples[dn] = sample

    samples = pd.DataFrame.from_dict(samples, orient='index')
    samples = samples.dropna()
    samples.to_csv('sample_paths.csv', index=False)
    print('\n', len(samples))
    print('DONE!')


class lc_seg_tiles_bc(Dataset):

    def __init__(self):

        self.samples = pd.read_csv('sample_paths.csv').sample(frac=args.subset / 100, random_state=0).reset_index(drop=True)
        if args.slides is not None:
            self.samples = self.samples[self.samples['Slide'].isin(args.slides.split('_'))]

        if args.cls > 0 and not args.classifier:
            self.samples = self.samples[self.samples['Classes'].str.contains(str(args.cls))]

        if args.augment:
            flips = [[0, 0], [0, 1], [1, 0], [1, 1]]
            rots = [0, 90, 180, 270]
            augs = []
            for r in rots:
                for f1, f2 in flips:
                    augs.append([f1, f2, r])

            num_augs = len(augs)
            augs *= len(self.samples)
            self.samples = pd.concat([self.samples] * num_augs, ignore_index=True)
            self.samples['Aug'] = augs
            self.samples = self.samples.sample(frac=1, random_state=0).reset_index(drop=True)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        s = self.samples.iloc[index]

        img = np.array(Image.open(s['Img']))[:dim, :dim]
        img = np.pad(img, ((0, dim - img.shape[0]), (0, dim - img.shape[1])), 'minimum')

        target = np.array(Image.open(s['Mask']))[:dim, :dim]
        target = np.pad(target, ((0, dim - target.shape[0]), (0, dim - target.shape[1])), 'minimum')
        orig_target = torch.Tensor(target.copy().astype(np.float32)).unsqueeze(0)

        if args.cls > 0:
            target[target != args.cls] = 0
        target[target > 0] = 1

        # img = normalise(img)
        img, target = torch.Tensor(img.astype(np.float32)).unsqueeze(0), torch.Tensor(target.astype(np.float32)).unsqueeze(0)

        if args.augment:
            hflip, vflip, angle = s['Aug']
            img = TF.rotate(img, angle)
            target = TF.rotate(target, angle)

            if hflip == 1:
                img = TF.hflip(img)
                target = TF.hflip(target)

            if vflip == 1:
                img = TF.vflip(img)
                target = TF.vflip(target)

        label = torch.max(target).unsqueeze(0)

        return img, target, label, orig_target


################################################ TRAINING


def run_epochs(net, train_loader, val_loader, cls_criterion, seg_criterion, optimizer, num_epochs, path, save_freq=100, train=True):
    if train:
        mode = 'train'
        dataloader = train_loader
    else:
        mode = 'val'
        net.eval()
        dataloader = val_loader

    for epoch in range(num_epochs):
        mean_loss = 0
        mean_scores = torch.zeros(4)
        for i, data in enumerate(dataloader):

            inputs, target_masks, target_labels, orig_targets = data
            inputs = inputs.to(device)
            target_masks, target_labels = target_masks.to(device), target_labels.to(device)
            output_masks, output_labels = net(inputs)
            output_masks, output_labels = torch.sigmoid(output_masks), torch.sigmoid(output_labels)

            target_masks = F.interpolate(target_masks, (output_masks.shape[-2], output_masks.shape[-1]))

            if args.classifier:
                loss_name = args.cls_lf
                loss = cls_criterion(output_labels, target_labels)

            else:
                loss_name = args.seg_lf
                loss = seg_criterion(output_masks, target_masks)

            if train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            mean_loss += loss.item()

            print('({}) Epoch: {}/{} Batch: {}/{} Batch {} {}'.format(mode, epoch, num_epochs, i, len(dataloader), loss_name, loss.item()))

            results = {
                '{}/batch'.format(mode):         i, '{}/epoch'.format(mode): epoch,
                '{}/{}'.format(mode, loss_name): loss.item(), '{}/mean_{}'.format(mode, loss_name): mean_loss / (i + 1)
                }

            if args.classifier:
                mean_scores += scores(output_labels, target_labels)
                results.update(dict(zip(['{}/cls/'.format(mode) + s for s in ['BCE', 'prec', 'rec', 'f1']], mean_scores / (i + 1))))
                print(target_labels[0], output_labels[0])
                correct = torch.sum(torch.round(output_labels) == torch.round(target_labels)) / args.batch_size
                print("{}% correct".format(correct * 100))
                results['{}/cls/correct_labels'.format(mode)] = correct.cpu()
                results["{}/cls/target_labels".format(mode)] = target_labels[0].cpu()
                results["{}/cls/output_labels".format(mode)] = output_labels[0].cpu()
            else:
                mean_scores += scores(output_masks, target_masks)
                results.update(dict(zip(['{}/seg/'.format(mode) + s for s in ['BCE', 'prec', 'rec', 'f1']], mean_scores / (i + 1))))

            if i % save_freq == 0:
                if not args.classifier:
                    results["{}/inputs".format(mode)] = [wandb.Image(i) for i in inputs.cpu()]
                    results["{}/seg/output_masks".format(mode)] = [wandb.Image(i) for i in output_masks.cpu()]
                    results["{}/seg/target_masks".format(mode)] = [wandb.Image(i) for i in target_masks.cpu()]
                    results["{}/seg/orig_targets".format(mode)] = [wandb.Image(i) for i in orig_targets.cpu()]
            wandb.log(results)

        if train:
            print('Saving model...')
            torch.save(net.state_dict(), path)
            print('Evaluating network...')
            with torch.no_grad():
                _ = run_epochs(net, None, eval_loader, cls_criterion, seg_criterion, None, 1, None, train=False, save_freq=args.save_freq)

    return net


dataset = lc_seg_tiles_bc()

if args.downsample:
    upsampling = 1
else:
    upsampling = 8

ap = dict(
        pooling='avg',  # one of 'avg', 'max'
        dropout=0.0,  # dropout ratio, default is None
        activation=None,  # activation function, default is None
        classes=1,  # define number of output labels
        )

if "PSP" in args.model:
    net = eval(args.model)(encoder_name=args.encoder, psp_out_channels=512, psp_use_batchnorm=True, psp_dropout=0.2, in_channels=1, classes=1,
                           activation=None, upsampling=upsampling, aux_params=ap)
else:
    net = eval(args.model)(encoder_name=args.encoder, in_channels=1, classes=1,
                           activation=None, aux_params=ap)

cls_criterion = eval(args.cls_lf)
seg_criterion = eval(args.seg_lf)

if torch.cuda.device_count() > 1:
    print("Using", torch.cuda.device_count(), "GPUs.")
    net = nn.DataParallel(net)

net.to(device)
num_epochs = args.epochs
optimizer = optim.AdamW(net.parameters(), lr=args.lr)
wandb.watch(net)
wandb.log({'samples': len(dataset)})

train_size = int(0.8 * len(dataset))
val_test_size = len(dataset) - train_size
val_size = int(0.5 * val_test_size)
test_size = val_test_size - val_size

train_data, val_test_data = torch.utils.data.random_split(dataset, [train_size, val_test_size])
val_data, test_data = torch.utils.data.random_split(val_test_data, [val_size, test_size])

train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, shuffle=True, worker_init_fn=np.random.seed(0), num_workers=0)
eval_loader = torch.utils.data.DataLoader(val_data, batch_size=args.batch_size, shuffle=True, worker_init_fn=np.random.seed(0), num_workers=0)

print('{} training samples, {} validation samples...'.format(train_size, val_size))

if args.load is not '':
    net.load_state_dict(torch.load('params/' + args.load + ".pth", map_location=torch.device(device)))

if args.train:
    print('Training network...')
    _ = run_epochs(net, train_loader, eval_loader, cls_criterion, seg_criterion, optimizer, num_epochs, 'params/' + params_id + '.pth', save_freq=min(train_size, args.save_freq))

else:
    with torch.no_grad():
        _ = run_epochs(net, None, eval_loader, cls_criterion, seg_criterion, None, 1, None, train=False, save_freq=args.save_freq)
