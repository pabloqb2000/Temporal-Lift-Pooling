import os
import pdb
import sys
import cv2
import copy
import json
import torch
import numpy as np
import torch.nn as nn
from tqdm import tqdm
import torch.nn.functional as F
import matplotlib.pyplot as plt
from evaluation.slr_eval.wer_calculation import evaluate
from torch.cuda.amp import autocast as autocast
from torch.cuda.amp import GradScaler

def seq_train(loader, model, optimizer, device, epoch_idx, recoder, loss_weights=None):
    model.train()
    loss_value = []
    total_loss_dict = {}    # dict of all types of loss
    for k in loss_weights.keys(): 
        total_loss_dict[k] = 0
    clr = [group['lr'] for group in optimizer.optimizer.param_groups]
    scaler = GradScaler()
    for batch_idx, data in enumerate(loader):
        vid = device.data_to_device(data[0])
        vid_lgt = device.data_to_device(data[1])
        label = device.data_to_device(data[2])
        label_lgt = device.data_to_device(data[3])
        optimizer.zero_grad()
        with autocast():
            ret_dict = model(vid, vid_lgt, label=label, label_lgt=label_lgt)
            loss, loss_dict = model.criterion_calculation(ret_dict, label, label_lgt)
        if np.isinf(loss.item()) or np.isnan(loss.item()):
            print('loss is nan')
            #print(data[-1])
            print(str(data[1])+'  frames')
            print(str(data[3])+'  glosses')
            continue
        scaler.scale(loss).backward()
        scaler.step(optimizer.optimizer)
        scaler.update()
        loss_value.append(loss.item())
        for item, value in loss_dict.items():
            total_loss_dict[item] += value
        if batch_idx % recoder.log_interval == 0:
            recoder.print_log(
                '\tEpoch: {}, Batch({}/{}) done. Loss: {:.8f}  lr:{:.6f}'
                    .format(epoch_idx, batch_idx, len(loader), loss.item(), clr[0]))
            for item, value in total_loss_dict.items():
                recoder.print_log(f'\t Mean {item} loss: {value/recoder.log_interval:.5f}')
            total_loss_dict = {}
            for k in loss_weights.keys(): 
                total_loss_dict[k] = 0
        del ret_dict
        del loss
    optimizer.scheduler.step()
    recoder.print_log('\tMean training loss: {:.10f}.'.format(np.mean(loss_value)))
    return loss_value


def seq_eval(cfg, loader, model, device, mode, epoch, work_dir, recoder):
    model.eval()
    total_sent = []
    total_info = []
    #save_file = {}
    stat = {i: [0, 0] for i in range(len(loader.dataset.dict))}
    print(f"Starting epoch, {len(loader)} batches")
    for batch_idx, data in enumerate(tqdm(loader)):
        save_dir = f"./json_saved_data/{mode}/{batch_idx}"
        if data[0] == None or os.path.exists(save_dir):
            continue
        recoder.record_timer("device")
        vid = device.data_to_device(data[0])
        vid_lgt = device.data_to_device(data[1])
        label = device.data_to_device(data[2])
        label_lgt = device.data_to_device(data[3])
        with torch.no_grad():
            try:
                ret_dict = model(vid, vid_lgt, label=label, label_lgt=label_lgt)
                save_ret_as_json(data, ret_dict, batch_idx, mode)
                del ret_dict
            except torch.cuda.OutOfMemoryError as e:
                print(e)
                print(" ------- ", batch_idx)
            finally:
                del vid, vid_lgt, label, label_lgt, data
        torch.cuda.empty_cache()
        # total_info += [file_name.split("|")[0] for file_name in data[-1]]
        # total_sent += ret_dict['recognized_sents']
    return 0
    try:
        write2file(work_dir + "output-hypothesis-{}.ctm".format(mode), total_info, total_sent)
        ret = evaluate(prefix=work_dir, mode=mode, output_file="output-hypothesis-{}.ctm".format(mode),
                       evaluate_dir=cfg.dataset_info['evaluation_dir'],
                       evaluate_prefix=cfg.dataset_info['evaluation_prefix'],
                       output_dir="epoch_{}_result/".format(epoch))
    except:
        print("Unexpected error:", sys.exc_info()[0])
        ret = "Percent Total Error       =  100.00%   (ERROR)"
        return float(ret.split("=")[1].split("%")[0])
    finally:
        pass
    recoder.print_log("Epoch {}, {} {}".format(epoch, mode, ret),
                      '{}/{}.txt'.format(work_dir, mode))
    return float(ret.split("=")[1].split("%")[0])

def save_ret_as_json(data, ret_dict, batch_idx, mode):
    save_dir = f"./json_saved_data/{mode}/{batch_idx}/"
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)

    npy_file = os.path.join(save_dir, "sequence_logits.npy")
    np.save(npy_file, ret_dict["sequence_logits"].cpu().numpy())
    
    save_dict = {
        "annotations": data[-1],
        "predictions": ret_dict["recognized_sents"],
        "labels": [int(n) for n in data[2].numpy()],
        "mode": mode
    }
    with open(os.path.join(save_dir, f"return_dict.json"), "w+") as f:
        json.dump(save_dict, f)


def seq_feature_generation(loader, model, device, mode, work_dir, recoder):
    model.eval()

    src_path = os.path.abspath(f"{work_dir}{mode}")
    tgt_path = os.path.abspath(f"./features/{mode}")

    if os.path.islink(tgt_path):
        curr_path = os.readlink(tgt_path)
        if work_dir[1:] in curr_path and os.path.isabs(curr_path):
            return
        else:
            os.unlink(tgt_path)
    else:
        if os.path.exists(src_path) and len(loader.dataset) == len(os.listdir(src_path)):
            os.symlink(src_path, tgt_path)
            return

    for batch_idx, data in tqdm(enumerate(loader)):
        recoder.record_timer("device")
        vid = device.data_to_device(data[0])
        vid_lgt = device.data_to_device(data[1])
        with torch.no_grad():
            ret_dict = model(vid, vid_lgt)
        if not os.path.exists(src_path):
            os.makedirs(src_path)
        start = 0
        for sample_idx in range(len(vid)):
            end = start + data[3][sample_idx]
            filename = f"{src_path}/{data[-1][sample_idx].split('|')[0]}_features.npy"
            save_file = {
                "label": data[2][start:end],
                "features": ret_dict['framewise_features'][sample_idx][:, :vid_lgt[sample_idx]].T.cpu().detach(),
            }
            np.save(filename, save_file)
            start = end

        os.symlink(src_path, tgt_path)
        assert end == len(data[2])


def write2file(path, info, output):
    filereader = open(path, "w")
    for sample_idx, sample in enumerate(output):
        for word_idx, word in enumerate(sample):
            filereader.writelines(
                "{} 1 {:.2f} {:.2f} {}\n".format(info[sample_idx],
                                                 word_idx * 1.0 / 100,
                                                 (word_idx + 1) * 1.0 / 100,
                                                 word[0]))
