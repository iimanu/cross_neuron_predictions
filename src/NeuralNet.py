# -*- coding: utf-8 -*-
import torch
import torch.nn
import torch.optim as optim
import torch.backends.cudnn as cudnn

# Utils:
import re
import os
import glob
import time
import math

from util.gen import Progbar, banner
import Config as cfg


# ----------------------------------------------------------------------------------------------------------------------
#
# ----------------------------------------------------------------------------------------------------------------------
class NeuralNet:
    def __init__(self):

        # Decide on device:
        if torch.cuda.is_available():
            self.device = torch.device('cuda')
            cudnn.benchmark = True
            if torch.cuda.device_count() > 1:
                raise AssertionError
                # This line enables multiple GPUs, but changes the layer names a bit
                # self.net = torch.nn.DataParallel(self.net)  # Useful if you have multiple GPUs - does not hurt otherwise
        else:
            self.device = torch.device('cpu')
            # torch.set_num_threads(4) # Presuming 4 cores
            print('WARNING: Found no valid GPU device - Running on CPU')
        # Build Model:
        print(f'==> Building model {cfg.NET.__name__} on the dataset {cfg.DATA_NAME}')
        self.net = cfg.NET(self.device)

        if cfg.RESUME_CHECKPOINT:
            print(f'==> Resuming from checkpoint via sorting method: {cfg.RESUME_METHOD}')
            assert os.path.isdir(cfg.CHECKPOINT_DIR), 'Error: no checkpoint directory found!'

            ck_file = self.__class__.resume_methods[cfg.RESUME_METHOD]()
            if ck_file is None:
                print(f'Found no valid checkpoints for {cfg.NET.__name__}')
                self.best_val_acc = 0
                self.start_epoch = 0
            else:
                checkpoint = torch.load(ck_file, map_location=self.device)
                self._load_checkpoint(checkpoint['net'])
                self.best_val_acc = checkpoint['acc']
                self.start_epoch = checkpoint['epoch']
                if 'dataset' in checkpoint:  # TODO - Remove this checkup
                    assert (cfg.DATA_NAME == checkpoint['dataset'])
                print(f'==> Loaded model with val-acc of {self.best_val_acc}')

        else:
            self.best_val_acc = 0
            self.start_epoch = 0

        self.net = self.net.to(self.device)

        # Build SGD Algorithm:
        self.criterion = torch.nn.CrossEntropyLoss()
        self.optimizer = optim.SGD(filter(lambda p: p.requires_grad, self.net.parameters()), lr=cfg.LEARN_RATE,
                                   momentum=0.9, weight_decay=5e-4)
        self.train_gen, self.val_gen, self.classes = (None, None, None)

    def train(self, epochs):

        # Bring in Data
        self.train_gen, self.val_gen, self.classes = cfg.TRAIN_GEN(batch_size=cfg.BATCH_SIZE,
                                                                   dataset_size=cfg.TRAIN_SET_SIZE,
                                                                   download=cfg.DO_DOWNLOAD)
        p = Progbar(epochs)
        t_start = time.time()
        batches_per_step = math.ceil(cfg.TRAIN_SET_SIZE / cfg.BATCH_SIZE)
        for epoch in range(self.start_epoch, self.start_epoch + epochs):

            if cfg.VERBOSITY > 0:
                banner(f'Epoch: {epoch}')
                t_step_start = time.time()
            train_loss, train_acc, train_count = self._train_step()
            val_loss, val_acc, val_count = self.test(self.val_gen)
            if cfg.VERBOSITY > 0:
                t_step_end = time.time()
                batch_time = round((t_step_end - t_step_start) / batches_per_step, 3)
                p.add(1, values=[("t_loss", train_loss), ("t_acc", train_acc), ("v_loss", val_loss), ("v_acc", val_acc),
                                 ("batch_time", batch_time)])
            else:
                p.add(1,
                      values=[("t_loss", train_loss), ("t_acc", train_acc), ("v_loss", val_loss), ("v_acc", val_acc)])
            self._checkpoint(val_acc, epoch + 1)
        t_end = time.time()
        print(f'==> Total train time: {t_end-t_start:.3f} secs :: per epoch: {(t_end-t_start)/epochs:.3f} secs')
        banner('Training Phase - End')

    def test(self, data_gen):
        self.net.eval()
        test_loss = 0
        correct = 0
        total = 0
        with torch.no_grad():  # TODO - Fix the odd intialize spatial layers bug
            for batch_idx, (inputs, targets) in enumerate(data_gen):
                inputs, targets = inputs.to(self.device), targets.to(self.device)
                outputs = self.net(inputs)
                loss = self.criterion(outputs, targets)

                test_loss += loss.item()
                _, predicted = outputs.max(1)
                total += targets.size(0)
                correct += predicted.eq(targets).sum().item()

                # val_loss = f'{test_loss/(batch_idx+1):.3f}.'
                # acc = f'{100.*correct/total:.3f}.'
                # count = f'{correct}/{total}'

        test_acc = 100. * correct / total
        count = f'{correct}/{total}'

        return test_loss, test_acc, count

    def summary(self, x_size, print_it=True):
        return self.net.summary(x_size, print_it=print_it)

    def print_weights(self):
        self.net.print_weights()

    def output_size(self, x_shape):
        return self.net.output_size(x_shape)

    def _checkpoint(self, val_acc, epoch):

        # Decide on whether to checkpoint or not:
        save_it = val_acc > self.best_val_acc
        if save_it and cfg.DONT_SAVE_REDUNDANT:
            checkpoints = [os.path.basename(f) for f in glob.glob(f'{cfg.CHECKPOINT_DIR}{cfg.NET.__name__}_*_ckpt.t7')]
            if checkpoints:
                best_cp_val_acc = max([float(f.replace(cfg.NET.__name__, '').split('_')[1]) for f in checkpoints])
                if best_cp_val_acc >= val_acc:
                    save_it = False
                    print(f'Resuming without save - Found valid checkpoint with higher val_acc: {best_cp_val_acc}')
        # Do checkpoint
        if save_it:
            print(f'\nBeat val_acc record of {self.best_val_acc} with {val_acc} - Saving checkpoint')
            state = {
                'net': self.net.state_dict(),
                'dataset': cfg.DATA_NAME,
                'acc': val_acc,
                'epoch': epoch,
            }
            if not os.path.isdir(cfg.CHECKPOINT_DIR):
                os.mkdir(cfg.CHECKPOINT_DIR)
            torch.save(state, f'{cfg.CHECKPOINT_DIR}{cfg.NET.__name__}_{val_acc}_ckpt.t7')
            self.best_val_acc = val_acc

    def _train_step(self):
        self.net.train()
        train_loss = 0
        correct = 0
        total = 0

        if cfg.VERBOSITY > 0:
            prog_batch = Progbar(len(self.train_gen))
        for batch_idx, (inputs, targets) in enumerate(self.train_gen):
            # Training step
            inputs, targets = inputs.to(self.device), targets.to(self.device)
            self.optimizer.zero_grad()
            outputs = self.net(inputs)
            loss = self.criterion(outputs, targets)
            loss.backward()
            self.optimizer.step()

            # Collect results:
            train_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()

            if cfg.VERBOSITY > 0:
                prog_batch.add(1, values=[("t_loss", train_loss / (batch_idx + 1)), ("t_acc", 100. * correct / total)])

        total_acc = 100. * correct / total
        count = f'{correct}/{total}'
        return train_loss, total_acc, count

    def _load_checkpoint(self, loaded_dict, optional_fill=('.*\.num_batches_tracked',),
                         total_ignore=('.*pred\.', '.*pred2\.', '.*pred1\.')):

        # Make a regex that matches if any of our regexes match.
        opt_fill = "(" + ")|(".join(optional_fill) + ")"
        tot_ignore = "(" + ")|(".join(total_ignore) + ")"

        curr_dict = self.net.state_dict()
        filtered_dict = {}

        for k, v in loaded_dict.items():
            if not re.match(tot_ignore, k):  # If in ignore list, ignore
                if k in curr_dict:
                    filtered_dict[k] = v
                else:  # Check if it is possible to ignore it being gone
                    if not re.match(opt_fill, k):
                        assert False, f'Fatal: found unknown entry {k} in loaded checkpoint'

        assert filtered_dict, 'State dictionary is empty'
        # Also check for missing entries in loaded checkpoint
        for k, v in curr_dict.items():
            if k not in loaded_dict and not (re.match(opt_fill, k) or re.match(tot_ignore, k)):
                assert False, f'Fatal: missing entry {k} from checkpoint'

        # Overwrite entries in the existing state dict
        curr_dict.update(filtered_dict)
        self.net.load_state_dict(curr_dict)

    @staticmethod
    def _find_top_val_acc_checkpoint():

        checkpoints = [os.path.basename(f) for f in glob.glob(f'{cfg.CHECKPOINT_DIR}{cfg.NET.__name__}_*_ckpt.t7')]
        if not checkpoints:
            return None
        else:
            checkpoints.sort(key=lambda x: x.replace(cfg.NET.__name__, '').split('_')[1])
            # print(checkpoints)
            return cfg.CHECKPOINT_DIR + checkpoints[-1]

    @staticmethod
    def _find_latest_checkpoint():

        checkpoints = glob.glob(f'{cfg.CHECKPOINT_DIR}{cfg.NET.__name__}_*_ckpt.t7')
        if not checkpoints:
            return None
        else:
            checkpoints.sort(key=os.path.getmtime)
            # print(checkpoints)
            return checkpoints[-1]

    # Static Variables
    resume_methods = {
        'Time': _find_latest_checkpoint.__func__,
        'ValAcc': _find_top_val_acc_checkpoint.__func__
    }
