from dataset.data_builder import build_data
from torchvision.utils import save_image
from tensorboardX import SummaryWriter
from loss import SSIM_Loss
import torch.nn as nn
from nets.generator import get_G
from nets.discriminator import get_D
from torch import autograd
from tqdm import tqdm
from model.flow import GLOW
import math
import cv2
import torch
import numpy as np
import yaml
import os
import argparse
from tools.coco_cut import classes as coco_classes
from tools.single_obj import SingleObj

log_file = None


def to_log(s, output=True):
    global log_file
    if output:
        print(s)
    print(s, file=log_file)


def open_config(root):
    f = open(os.path.join(root, "config.yaml"))
    config = yaml.load(f, Loader=yaml.FullLoader)
    return config


def load(models, epoch, root):
    def _detect_latest():
        checkpoints = os.listdir(os.path.join(root, "logs"))
        checkpoints = [f for f in checkpoints if f.startswith("G_epoch-") and f.endswith(".pth")]
        checkpoints = [int(f[len("G_epoch-"):-len(".pth")]) for f in checkpoints]
        checkpoints = sorted(checkpoints)
        _epoch = checkpoints[-1] if len(checkpoints) > 0 else None
        return _epoch

    if epoch == -1:
        epoch = _detect_latest()
    if epoch is None:
        return -1
    for name, model in models.items():
        ckpt = torch.load(os.path.join(root, "logs/" + name + "_epoch-{}.pth".format(epoch)))
        ckpt = {k: v for k, v in ckpt.items()}
        model.load_state_dict(ckpt)
        to_log("load model: {} from epoch: {}".format(name, epoch))
    # print("loaded from epoch: {}".format(epoch))
    return epoch


def calc_gradient_penalty(netD, origin, fake_data, batch_size, gp_lambda):
    alpha = torch.rand(batch_size, 1, 1, 1)
    alpha = alpha.expand(origin.shape).contiguous()
    alpha = alpha.cuda()

    interpolates = alpha * origin + ((1 - alpha) * fake_data)

    interpolates = interpolates.cuda()
    interpolates.requires_grad = True

    disc_interpolates = netD(interpolates)

    gradients = autograd.grad(outputs=disc_interpolates, inputs=interpolates,
                              grad_outputs=torch.ones(disc_interpolates.size()).cuda(),
                              create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradients = gradients.view(gradients.shape[0], -1)

    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean() * gp_lambda
    return gradient_penalty


# BCHW
def batch_image_merge(image):
    # image = torch.cat(image.split(4, 0), 2)
    image = torch.cat(image.split(1, 0), 3)
    # CH'W'
    return image


def imagetensor2np(x):
    x = torch.round((x + 1) / 2 * 255).clamp(0, 255).int().abs()
    x = x.detach().cpu().numpy()
    x = np.array(x, dtype=np.uint8).squeeze(0)
    x = np.transpose(x, [1, 2, 0])
    return x


def train(args, root):
    global log_file
    if not os.path.exists(os.path.join(root, "logs")):
        os.mkdir(os.path.join(root, "logs"))
    if not os.path.exists(os.path.join(root, "logs/result/")):
        os.mkdir(os.path.join(root, "logs/result/"))
    if not os.path.exists(os.path.join(root, "logs/result/event")):
        os.mkdir(os.path.join(root, "logs/result/event"))
    log_file = open(os.path.join(root, "logs/log.txt"), "w")
    to_log(args)
    writer = SummaryWriter(os.path.join(root, "logs/result/event/"))

    if args['classes'] == 'NONE':
        args['classes'] = list(coco_classes.keys())
    classes_num = len(args['classes'])

    if classes_num == 1:
        single_root = '../experiments/pix2pix_person'
    elif classes_num == 5:
        single_root = '../experiments/pix2pix_5class_new_nfl'
    # elif classes_num == 10:
    #    single_root = '../experiments/p2p_10class'
    else:
        assert 0
    single_model = SingleObj(open_config(single_root), single_root)
    data_root = os.path.join(args['data_path'], "COCO", "results_coco_train_{}".format(classes_num))
    dataloader = build_data(args['data_tag'], data_root, args["bs"], True, num_worker=args["num_workers"],
                            classes=args['classes'], image_size=args['image_size'], obj_model=single_model)

    G = get_G("post", in_channels=3, out_channels=3, scale=6).cuda()
    D = get_D("post", classes=2).cuda()

    g_opt = torch.optim.Adam(G.parameters(), lr=args["lr"], betas=(0.5, 0.9))
    d_opt = torch.optim.Adam(D.parameters(), lr=args["lr"], betas=(0.5, 0.9))
    g_sch = torch.optim.lr_scheduler.MultiStepLR(g_opt, args["lr_milestone"], gamma=0.5)
    d_sch = torch.optim.lr_scheduler.MultiStepLR(d_opt, args["lr_milestone"], gamma=0.5)

    load_epoch = load({"G": G, "g_opt": g_opt, "g_sch": g_sch, "D": D, "d_opt": d_opt, "d_sch": d_sch},
                      args["load_epoch"], root)
    tot_iter = (load_epoch + 1) * len(dataloader)

    max_iter_per_epoch = args['max_iter_per_epoch']
    if max_iter_per_epoch < 1:
        max_iter_per_epoch = len(dataloader.dataset)
    g_opt.step()
    d_opt.step()
    for epoch in range(load_epoch + 1, args['epoch']):
        g_sch.step()
        d_sch.step()
        for i, (synthesis, origin, shapes) in enumerate(dataloader):
            if i >= max_iter_per_epoch:
                break
            tot_iter += 1
            synthesis, origin, shapes = synthesis.cuda(), origin.cuda(), shapes.cuda()
            for _ in range(0, args['D_iter']):
                d_opt.zero_grad()
                # D_real
                pvalidity = D(origin)
                D_loss_real_val = -pvalidity.mean()
                D_loss_real = D_loss_real_val
                # D_fake
                G_out = G(synthesis)
                pvalidity = D(G_out)
                D_loss_fake_val = pvalidity.mean()
                D_loss_fake = D_loss_fake_val

                # wgan-gp
                gradient_penalty = calc_gradient_penalty(D, origin, G_out.detach(), origin.shape[0], args['gp_lambda'])

                # D-cost
                D_loss = D_loss_fake + D_loss_real + gradient_penalty
                D_loss.backward()
                d_opt.step()

            g_opt.zero_grad()
            # G
            G_out = G(synthesis)
            pvalidity = D(G_out)
            l1_loss = (nn.L1Loss().cuda())(G_out, origin)
            G_loss_val = -pvalidity.mean()

            G_loss = G_loss_val + l1_loss * args['lambda_l1']
            G_loss.backward()

            g_opt.step()

            if tot_iter % args['show_interval'] == 0:
                to_log(
                    'epoch: {}, batch: {}, D_loss: {:.5f}, D_loss_real: {:.5f}, D_loss_fake: {:.5f},' \
                    'D_loss_real_val: {:.5f},  D_loss_fake_val: {:.5f},' \
                    'G_loss: {:5f}, G_loss_val: {:.5f},' \
                    'l1: {:5f}, gradient_penalty: {:5f}, lr: {:.5f}'.format(
                        epoch, i, D_loss.item(), D_loss_real.item(), D_loss_fake.item(), D_loss_real_val.item(),
                        D_loss_fake_val.item(), G_loss.item(),
                        G_loss_val.item(), l1_loss.item(), gradient_penalty.item(),
                        g_sch.get_last_lr()[0]))
                writer.add_scalar("loss/D_loss", D_loss.item(), tot_iter)
                writer.add_scalar("loss/D_loss_real", D_loss_real.item(), tot_iter)
                writer.add_scalar("loss/D_loss_fake", D_loss_fake.item(), tot_iter)
                writer.add_scalar("loss/D_loss_real_val", D_loss_real_val.item(), tot_iter)
                writer.add_scalar("loss/D_loss_fake_val", D_loss_fake_val.item(), tot_iter)
                writer.add_scalar("loss/G_loss", G_loss.item(), tot_iter)
                writer.add_scalar("loss/G_loss_val", G_loss_val.item(), tot_iter)
                writer.add_scalar("loss/l1", l1_loss.item(), tot_iter)
                writer.add_scalar("loss/gradient_penalty", gradient_penalty.item(), tot_iter)
                writer.add_scalar("lr", g_sch.get_last_lr()[0], tot_iter)

        if epoch % args["snapshot_interval"] == 0:
            torch.save(G.state_dict(), os.path.join(root, "logs/G_epoch-{}.pth".format(epoch)))
            torch.save(D.state_dict(), os.path.join(root, "logs/D_epoch-{}.pth".format(epoch)))
            torch.save(g_opt.state_dict(), os.path.join(root, "logs/g_opt_epoch-{}.pth".format(epoch)))
            torch.save(d_opt.state_dict(), os.path.join(root, "logs/d_opt_epoch-{}.pth".format(epoch)))
            torch.save(g_sch.state_dict(), os.path.join(root, "logs/g_sch_epoch-{}.pth".format(epoch)))
            torch.save(d_sch.state_dict(), os.path.join(root, "logs/d_sch_epoch-{}.pth".format(epoch)))
        if epoch % args['test_interval'] == 0:
            # label = torch.tensor([5]).expand([64]).cuda()
            # input_test[:, 1, :, :] = label.reshape(64, 1, 1).expand(64, image_size, image_size)
            # G_out = G(input_test)
            image = G_out.clone().detach()
            image = batch_image_merge(image)
            image = imagetensor2np(image)
            origin = batch_image_merge(origin)
            origin = imagetensor2np(origin)
            G_out = G_out / 2 + 0.5
            G_out = G_out.clamp(0, 1)
            save_image(G_out, os.path.join(root, "logs/output-{}.png".format(epoch)))
            writer.add_image('image{}/mask'.format(epoch), origin, tot_iter, dataformats='HWC')
            writer.add_image('image{}/fake'.format(epoch), image, tot_iter,
                             dataformats='HWC')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=str)
    parser.add_argument("--test", default=False, action='store_true')
    args = parser.parse_args()
    train(open_config(args.root), args.root)
