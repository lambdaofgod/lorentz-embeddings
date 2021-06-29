import os
import sys
import torch
import random
import numpy as np
from torch import nn
from torch import optim
from tqdm import trange, tqdm
from collections import Counter
from datetime import datetime
from tensorboardX import SummaryWriter
from torch.utils.data import Dataset, DataLoader

import matplotlib

matplotlib.use("Agg")  # this needs to come before other matplotlib imports
import matplotlib.pyplot as plt

plt.style.use("ggplot")


def arcosh(x):
    return torch.log(x + torch.sqrt(x ** 2 - 1))


def lorentz_scalar_product(x, y):
    # BD, BD -> B
    m = x * y
    result = m[:, 1:].sum(dim=1) - m[:, 0]
    return result


def tangent_norm(x):
    # BD -> B
    return torch.sqrt(lorentz_scalar_product(x, x))


def exp_map(x, v):
    # BD, BD -> BD
    tn = tangent_norm(v).unsqueeze(dim=1)
    tn_expand = tn.repeat(1, x.size()[-1])
    result = torch.cosh(tn) * x + torch.sinh(tn) * (v / tn)
    result = torch.where(tn_expand > 0, result, x)  # only update if tangent norm is > 0
    return result


def set_dim0(x):
    x = torch.renorm(x, p=2, dim=0, maxnorm=1e2)  # otherwise leaves will explode
    # NOTE: the paper does not mention the square part of the equation but if
    # you try to derive it you get a square term in the equation
    dim0 = torch.sqrt(1 + (x[:, 1:] ** 2).sum(dim=1))
    x[:, 0] = dim0
    return x


# ========================= models


class RSGD(optim.Optimizer):
    def __init__(self, params, learning_rate=None):
        learning_rate = learning_rate if learning_rate is not None else 0.01
        defaults = {"learning_rate": learning_rate}
        super().__init__(params, defaults=defaults)

    def step(self):
        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                B, D = p.size()
                gl = torch.eye(D, device=p.device, dtype=p.dtype)
                gl[0, 0] = -1
                grad_norm = torch.norm(p.grad.data)
                grad_norm = torch.where(grad_norm > 1, grad_norm, torch.tensor(1.0).to(p.device))
                # only normalize if global grad_norm is more than 1
                h = (p.grad.data / grad_norm) @ gl
                proj = (
                    h
                    - (
                        lorentz_scalar_product(p, h) / lorentz_scalar_product(p, p)
                    ).unsqueeze(1)
                    * p
                )
                # print(p, lorentz_scalar_product(p, p))
                update = exp_map(p, -group["learning_rate"] * proj)
                is_nan_inf = torch.isnan(update) | torch.isinf(update)
                update = torch.where(is_nan_inf, p, update)
                update[0, :] = p[0, :]  # no ❤️  for embedding
                update = set_dim0(update)
                p.data.copy_(update)


class Lorentz(nn.Module):
    """
    This will embed `n_items` in a `dim` dimensional lorentz space.
    """

    def __init__(self, n_items, dim, init_range=0.001):
        super().__init__()
        self.n_items = n_items
        self.dim = dim
        self.table = nn.Embedding(n_items + 1, dim, padding_idx=0)
        nn.init.uniform_(self.table.weight, -init_range, init_range)
        # equation 6
        with torch.no_grad():
            self.table.weight[0] = 5  # padding idx push it to corner
            set_dim0(self.table.weight)

    def forward(self, I, Ks):
        """
        Using the pairwise similarity matrix, generate the following inputs and
        provide to this function.

        Inputs:
            - I     :   - long tensor
                        - size (B,)
                        - This denotes the `i` used in all equations.
            - Ks    :   - long tensor
                        - size (B, N)
                        - This denotes at max `N` documents which come from the
                          nearest neighbor sample.
                        - The `j` document must be the first of the N indices.
                          This is used to calculate the losses
        Return:
            - size (B,)
            - Ranking loss calculated using
              document to the given `i` document.

        """
        n_ks = Ks.size()[1]
        ui = torch.stack([self.table(I)] * n_ks, dim=1)
        uks = self.table(Ks)
        # ---------- reshape for calculation
        B, N, D = ui.size()
        ui = ui.reshape(B * N, D)
        uks = uks.reshape(B * N, D)
        dists = -lorentz_scalar_product(ui, uks)
        dists = torch.where(dists <= 1, torch.ones_like(dists) + 1e-6, dists)
        # sometimes 2 embedding can come very close in R^D.
        # when calculating the lorenrz inner product,
        # -1 can become -0.99(no idea!), then arcosh will become nan
        dists = -arcosh(dists)
        # print(dists)
        # ---------- turn back to per-sample shape
        dists = dists.reshape(B, N)
        loss = -(dists[:, 0] - torch.log(torch.exp(dists).sum(dim=1) + 1e-6))
        return loss

    def lorentz_to_poincare(self):
        table = self.table.weight.data.cpu().numpy()
        return table[:, 1:] / (
            table[:, :1] + 1
        )  # diffeomorphism transform to poincare ball

    def get_lorentz_table(self):
        return self.table.weight.data.cpu().numpy()

    def _test_table(self):
        x = self.table.weight.data
        check = lorentz_scalar_product(x, x) + 1.0
        return check.cpu().numpy().sum()


class Graph(Dataset):
    def __init__(self, pairwise_matrix, sample_size=10):
        self.pairwise_matrix = pairwise_matrix
        self.n_items = len(pairwise_matrix)
        self.sample_size = sample_size
        self.arange = np.arange(0, self.n_items)
        self.cnter = 0

    def __len__(self):
        return self.n_items

    def __getitem__(self, i):
        self.cnter = (self.cnter + 1) % self.sample_size
        I = torch.Tensor([i + 1]).squeeze().long()
        has_child = (self.pairwise_matrix[i] > 0).sum()
        has_parent = (self.pairwise_matrix[:, i] > 0).sum()
        if self.cnter == 0:
            arange = np.random.permutation(self.arange)
        else:
            arange = self.arange

        if has_parent:  # if no child go for parent
            valid_idxs = arange[self.pairwise_matrix[arange, i] > 0]
            j = valid_idxs[0]
            min = self.pairwise_matrix[j,i]
        elif has_child:
            valid_idxs = arange[self.pairwise_matrix[i, arange] > 0]
            j = valid_idxs[0]
            min = self.pairwise_matrix[i,j]
        else:
            raise Exception(f"Node {i} has no parent and no child")
        indices = arange
        indices = indices[indices != i]
        if has_child:
            indices = indices[self.pairwise_matrix[i,indices] < min]
        else:
            indices = indices[self.pairwise_matrix[indices, i] < min]

        indices = indices[: self.sample_size]
        #print(indices)
        #raise NotImplementedError()
        Ks = np.concatenate([[j], indices, np.zeros(self.sample_size)])[
            : self.sample_size
        ]
        # print(I, Ks)
        return I, torch.Tensor(Ks).long()


def recon(table, pair_mat):
    "Reconstruction accuracy"
    count = 0
    table = torch.tensor(table[1:])
    for i in range(1, len(pair_mat)):  # 0 padding, 1 root, we leave those two
        x = table[i].repeat(len(table)).reshape([len(table), len(table[i])])  # N, D
        mask = torch.tensor([0.0] * len(table))
        mask[i] = 1
        mask = mask * -10000.0
        dists = lorentz_scalar_product(x, table) + mask
        dists = (
            dists.cpu().numpy()
        )  # arccosh is monotonically increasing, so no need of that here
        # and no -dist also, as acosh in m i, -acosh(-l(x,y)) is nothing but l(x,y)
        # print(dists)
        predicted_parent = np.argmax(dists)
        actual_parent = np.argmax(pair_mat[:, i])
        # print(predicted_parent, actual_parent, i, end="\n\n")
        count += actual_parent == predicted_parent
    count = count / (len(pair_mat) - 1) * 100
    return count


_moon_count = 0


def _moon(loss, phases="🌕🌖🌗🌘🌑🌒🌓🌔"):
    global _moon_count
    _moon_count += 1
    p = phases[_moon_count % 8]
    return f"{p} Loss: {float(loss)}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", help="File:pairwise_matrix")
    parser.add_argument(
        "-sample_size", help="How many samples in the N matrix", default=5, type=int
    )
    parser.add_argument(
        "-batch_size", help="How many samples in the batch", default=32, type=int
    )
    parser.add_argument(
        "-burn_c",
        help="Divide learning rate by this for the burn epochs",
        default=10,
        type=int,
    )
    parser.add_argument(
        "-burn_epochs",
        help="How many epochs to run the burn phase for?",
        default=100,
        type=int,
    )
    parser.add_argument(
        "-plot", help="Plot the embeddings", default=False, action="store_true"
    )
    parser.add_argument("-plot_size", help="Size of the plot", default=3, type=int)
    parser.add_argument(
        "-plot_graph",
        help="Plot the Graph associated with the embeddings",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "-overwrite_plots",
        help="Overwrite the plots?",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "-ckpt", help="Which checkpoint to use?", default=None, type=str
    )
    parser.add_argument(
        "-shuffle", help="Shuffle within batch while learning?", default=True, type=bool
    )
    parser.add_argument(
        "-epochs", help="How many epochs to optimize for?", default=1_000_000, type=int
    )
    parser.add_argument(
        "-poincare_dim",
        help="Poincare projection time. Lorentz will be + 1",
        default=2,
        type=int,
    )
    parser.add_argument(
        "-n_items", help="How many items to embed?", default=None, type=int
    )
    parser.add_argument(
        "-learning_rate", help="RSGD learning rate", default=0.1, type=float
    )
    parser.add_argument(
        "-log_step", help="Log at what multiple of epochs?", default=1, type=int
    )
    parser.add_argument(
        "-logdir", help="What folder to put logs in", default="runs", type=str
    )
    parser.add_argument(
        "-save_step", help="Save at what multiple of epochs?", default=100, type=int
    )
    parser.add_argument(
        "-savedir", help="What folder to put checkpoints in", default="ckpt", type=str
    )
    parser.add_argument(
        "-loader_workers",
        help="how many workers to generate tensors",
        default=4,
        type=int,
    )

    parser.add_argument(
        "-device",
        default='cuda'
    )
    args = parser.parse_args()
    # ----------------------------------- get the correct matrix
    if not os.path.exists(args.logdir):
        os.mkdir(args.logdir)
    if not os.path.exists(args.savedir):
        os.mkdir(args.savedir)

    exec(f"from datasets import {args.dataset} as pairwise")
    pairwise = pairwise[: args.n_items, : args.n_items]
    args.n_items = len(pairwise) if args.n_items is None else args.n_items
    print(f"{args.n_items} being embedded")

    # ---------------------------------- Generate the proper objects
    net = Lorentz(
        args.n_items, args.poincare_dim + 1
    ).to(args.device)  # as the paper follows R^(n+1) for this space
    if args.plot:
        if args.poincare_dim != 2:
            print("Only embeddings with `-poincare_dim` = 2 are supported for now.")
            sys.exit(1)
        if args.ckpt is None:
            print("Please provide `-ckpt` when using `-plot`")
            sys.exit(1)
        if os.path.isdir(args.ckpt):
            paths = [
                os.path.join(args.ckpt, c)
                for c in os.listdir(args.ckpt)
                if c.endswith("ckpt")
            ]
        else:
            paths = [args.ckpt]
        paths = list(sorted(paths))
        edges = [
            tuple(edge)
            for edge in set(
                [
                    frozenset((a + 1, b + 1))
                    for a, row in enumerate(pairwise > 0)
                    for b, is_non_zero in enumerate(row)
                    if is_non_zero
                ]
            )
        ]
        print(len(edges), "nodes")
        internal_nodes = set(
            node
            for node, count in Counter(
                [node for edge in edges for node in edge]
            ).items()
            if count > 1
        )
        edges = np.array([edge for edge in edges if edge[1] in internal_nodes])
        print(len(edges), "internal nodes")
        for path in tqdm(paths, desc="Plotting"):
            save_path = f"{path}.svg"
            if os.path.exists(save_path) and not args.overwrite_plots:
                continue
            net.load_state_dict(torch.load(path))
            table = net.lorentz_to_poincare()
            # skip padding. plot x y
            plt.figure(figsize=(7, 7))
            if args.plot_graph:
                for edge in edges:
                    plt.plot(
                        table[edge, 0],
                        table[edge, 1],
                        color="black",
                        marker="o",
                        alpha=0.5,
                    )
            else:
                plt.scatter(table[1:, 0], table[1:, 1])
            plt.title(path)
            plt.gca().set_xlim(-1, 1)
            plt.gca().set_ylim(-1, 1)
            plt.gca().add_artist(plt.Circle((0, 0), 1, fill=False, edgecolor="black"))
            plt.savefig(save_path)
            plt.close()
        sys.exit(0)

    dataloader = DataLoader(
        Graph(pairwise, args.sample_size),
        shuffle=args.shuffle,
        batch_size=args.batch_size,
        num_workers=args.loader_workers,
    )
    rsgd = RSGD(net.parameters(), learning_rate=args.learning_rate)

    name = f"{args.dataset}  {datetime.utcnow()}"
    writer = SummaryWriter(f"{args.logdir}/{name}")

    with tqdm(ncols=80, mininterval=0.2, total=args.epochs) as epoch_bar:
        for epoch in range(args.epochs):
            rsgd.learning_rate = (
                args.learning_rate / args.burn_c
                if epoch < args.burn_epochs
                else args.learning_rate
            )
            for I, Ks in dataloader:
                I = I.to(args.device)
                Ks = Ks.to(args.device)
                rsgd.zero_grad()
                loss = net(I, Ks).mean()
                loss.backward()
                rsgd.step()
            writer.add_scalar("loss", loss, epoch)
            writer.add_scalar(
                "recon_preform", recon(net.get_lorentz_table(), pairwise), epoch
            )
            writer.add_scalar("table_test", net._test_table(), epoch)
            if epoch % args.save_step == 0:
                torch.save(net.state_dict(), f"{args.savedir}/{epoch} {name}.ckpt")
            epoch_bar.set_description(
                f"🔥 Burn phase loss: {float(loss)}"
                if epoch < args.burn_epochs
                else _moon(loss)
            )
            epoch_bar.update(1)
