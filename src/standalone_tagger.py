#!/usr/bin/env python
from __future__ import print_function
from __future__ import unicode_literals
import os
import h5py
import errno
import sys
import codecs
import argparse
import time
import random
import logging
import json
import tempfile
import collections
import torch
import subprocess
from modules.gal_lstm import GalLSTM
from seqlabel.crf_layer import CRFLayer
from seqlabel.partial_crf_layer import PartialCRFLayer
logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s: %(message)s')


def dict2namedtuple(dic):
  return collections.namedtuple('Namespace', dic.keys())(**dic)


def read_corpus(path):
  """
  read CoNLL format data.

  :param path:
  :return:
  """
  dataset = []
  labels_dataset = []
  with codecs.open(path, 'r', encoding='utf-8') as fin:
    for lines in fin.read().strip().split('\n\n'):
      items, labels = [], []
      for line in lines.splitlines():
        label, item = line.split()
        items.append(item)
        labels.append(label)

      dataset.append(items)
      labels_dataset.append(labels)
  return dataset, labels_dataset


def create_one_batch(dim, n_layers, raw_data, raw_labels, lexicon, sort=True, use_cuda=False):
  batch_size = len(raw_data)
  lst = list(range(batch_size))
  if sort:
    lst.sort(key=lambda l: -len(raw_data[l]))

  sorted_raw_data = [raw_data[i] for i in lst]
  sorted_raw_labels = [raw_labels[i] for i in lst]
  sorted_lens = [len(raw_data[i]) for i in lst]
  max_len = max(sorted_lens)

  batch_x = torch.FloatTensor(batch_size, max_len, n_layers, dim).fill_(0)
  batch_y = torch.LongTensor(batch_size, max_len).fill_(0)
  for i, raw_items in enumerate(sorted_raw_data):
    # new_raw_items = ['<bos>'] + raw_items + ['<eos>']
    new_raw_items = raw_items
    sentence_key = '\t'.join(new_raw_items).replace('.', '$period$').replace('/', '$backslash$')
    batch_x[i][: sorted_lens[i]] = torch.from_numpy(lexicon[sentence_key][()]).transpose(0, 1)
    for j, _ in enumerate(raw_items):
      batch_y[i][j] = sorted_raw_labels[i][j]

  if use_cuda:
    batch_x = batch_x.cuda()
    batch_y = batch_y.cuda()

  return batch_x, batch_y, sorted_lens


# shuffle training examples and create mini-batches
def create_batches(dim, n_layers, raw_data, raw_labels, lexicon, batch_size,
                   perm=None, shuffle=True, sort=True, keep_full=False, use_cuda=False):
  lst = perm or list(range(len(raw_data)))
  if shuffle:
    random.shuffle(lst)

  if sort:
    lst.sort(key=lambda l: -len(raw_data[l]))

  sorted_raw_data = [raw_data[i] for i in lst]
  sorted_raw_labels = [raw_labels[i] for i in lst]

  sum_len = 0.0
  batches_x, batches_y, batches_lens = [], [], []
  size = batch_size
  n_batch = (len(raw_data) - 1) // size + 1

  start_id = 0
  while start_id < len(raw_data):
    end_id = start_id + size
    if end_id > len(raw_data):
      end_id = len(raw_data)

    if keep_full and len(sorted_raw_data[start_id]) != len(sorted_raw_data[end_id - 1]):
      end_id = start_id + 1
      while end_id < len(raw_data) and len(sorted_raw_data[end_id]) == len(sorted_raw_data[start_id]):
        end_id += 1

    bx, by, blens = create_one_batch(dim, n_layers,
                                     sorted_raw_data[start_id: end_id], sorted_raw_labels[start_id: end_id],
                                     lexicon,
                                     sort=sort, use_cuda=use_cuda)
    sum_len += sum(blens)
    batches_x.append(bx)
    batches_y.append(by)
    batches_lens.append(blens)
    start_id = end_id
    
  logging.info("{} batches, avg len: {:.1f}".format(n_batch, sum_len / len(raw_data)))
  order = [0] * len(lst)
  for i, l in enumerate(lst):
    order[l] = i
  return batches_x, batches_y, batches_lens, order


class Model(torch.nn.Module):
  def __init__(self, opt, dim, n_layers, n_class, consider_word_piece, use_cuda):
    super(Model, self).__init__()
    self.use_cuda = use_cuda
    self.opt = opt

    encoder_input_dim = dim

    if opt.encoder.lower() == 'lstm':
      self.encoder = torch.nn.LSTM(encoder_input_dim,
                                   opt.hidden_dim, num_layers=opt.depth, bidirectional=True,
                                   batch_first=True, dropout=opt.dropout)
      encoder_output_dim = opt.hidden_dim * 2
    elif opt.encoder.lower() == 'gal_lstm':
      self.encoder = GalLSTM(encoder_input_dim, opt.hidden_dim, num_layers=opt.depth,
                             bidirectional=True,
                             wdrop=opt.dropout, idrop=opt.dropout, batch_first=True)
      encoder_output_dim = opt.hidden_dim * 2
    else:
      raise ValueError('Unknown encoder name: {}'.format(opt.encoder.lower()))

    weights = torch.randn(n_layers)
    self.weights = torch.nn.Parameter(weights, requires_grad=True)

    # CRF: as suggested by Reimers and Gurevych [2017]
    if not consider_word_piece:
      self.classify_layer = CRFLayer(encoder_output_dim, n_class, use_cuda)
    else:
      self.classify_layer = PartialCRFLayer(encoder_output_dim, n_class, use_cuda)
    self.train_time = 0
    self.eval_time = 0
    self.emb_time = 0
    self.classify_time = 0

  def forward(self, x, y):
    x = torch.autograd.Variable(x, requires_grad=False)
    x = x.transpose(-2, -1).matmul(self.weights)
    output, hidden = self.encoder(x)

    start_time = time.time()
    output, loss = self.classify_layer.forward(output, y)

    if not self.training:
      self.classify_time += time.time() - start_time

    if self.training:
      loss += self.opt.l2 * self.classify_layer.hidden2tag.weight.data.norm(2)

    return output, loss


def eval_model(model, valid_package, ix2label, args, gold_path):
  if args.output is not None:
    path = args.output
    fpo = codecs.open(path, 'w', encoding='utf-8')
  else:
    descriptor, path = tempfile.mkstemp(suffix='.tmp')
    fpo = codecs.getwriter('utf-8')(os.fdopen(descriptor, 'w'))

  valid_x, valid_y, valid_lens, order = valid_package

  model.eval()
  tagset = []
  for x, y, lens in zip(valid_x, valid_y, valid_lens):
    output, loss = model.forward(x, y)
    output_data = output.data
    for bid in range(len(x)):
      tags = []
      for k in range(lens[bid]):
        tag = ix2label[int(output_data[bid][k])]
        tags.append(tag)
      tagset.append(tags)
  for l in order:
    for tag in tagset[l]:
      print(tag, file=fpo)
    print(file=fpo)
  fpo.close()

  model.train()
  p = subprocess.Popen([args.script, gold_path, path], stdout=subprocess.PIPE)
  p.wait()
  f = 0
  for line in p.stdout.readlines():
    f = line.strip().split()[-1]
  os.remove(path)
  return float(f)


def train_model(epoch, model, optimizer,
                train_package, valid_package, test_package, ix2label, best_valid, test_result):
  model.train()
  opt = model.opt

  total_loss, total_tag = 0.0, 0
  cnt = 0
  start_time = time.time()

  train_x, train_y, train_lens, _ = train_package
  lst = list(range(len(train_x)))
  random.shuffle(lst)
  train_x = [train_x[l] for l in lst]
  train_y = [train_y[l] for l in lst]
  train_lens = [train_lens[l] for l in lst]

  logging.info('Going to evaluate every {} steps.'.format(opt.eval_steps))
  for x, y, lens in zip(train_x, train_y, train_lens):
    cnt += 1
    model.zero_grad()
    _, loss = model.forward(x, y)
    total_loss += loss.item()
    n_tags = sum(lens)
    total_tag += n_tags
    loss.backward()

    # gradient normalization: as suggested by Reimers and Gurevych [2017]
    torch.nn.utils.clip_grad_norm_(model.parameters(), opt.clip_grad)
    optimizer.step()

    if cnt * opt.batch_size % 1024 == 0:
      logging.info("Epoch={} iter={} lr={:.6f} train_ave_loss={:.6f} time={:.2f}s".format(
        epoch, cnt, optimizer.param_groups[0]['lr'],
        1.0 * loss.data[0] / n_tags, time.time() - start_time
      ))
      start_time = time.time()

    if cnt % opt.eval_steps == 0:
      valid_result = eval_model(model, valid_package, ix2label, opt, opt.gold_valid_path)
      logging.info("Epoch={} iter={} lr={:.6f} train_loss={:.6f} valid_acc={:.6f}".format(
        epoch, cnt, optimizer.param_groups[0]['lr'], total_loss, valid_result))

      if valid_result > best_valid:
        torch.save(model.state_dict(), os.path.join(opt.model, 'model.pkl'))
        logging.info("New record achieved!")
        best_valid = valid_result
        if test_package is not None:
          test_result = eval_model(model, test_package, ix2label, opt, opt.gold_test_path)
          logging.info("Epoch={} iter={} lr={:.6f} test_acc={:.6f}".format(
            epoch, cnt, optimizer.param_groups[0]['lr'], test_result))    

  return best_valid, test_result


def label_to_index(y, label_to_ix, incremental=True):
  for i in range(len(y)):
    for j in range(len(y[i])):
      if y[i][j] not in label_to_ix and incremental:
        label = label_to_ix[y[i][j]] = len(label_to_ix)
      else:
        label = label_to_ix.get(y[i][j], 0)
      y[i][j] = label


def train():
  cmd = argparse.ArgumentParser(sys.argv[0], conflict_handler='resolve')
  cmd.add_argument('--seed', default=1, type=int, help='the random seed.')
  cmd.add_argument('--gpu', default=-1, type=int, help='use id of gpu, -1 if cpu.')
  cmd.add_argument('--encoder', default='gal_lstm', choices=['lstm', 'gal_lstm'],
                   help='the type of encoder: valid options=[lstm, gal_lstm]')
  cmd.add_argument('--optimizer', default='sgd', choices=['sgd', 'adam'],
                   help='the type of optimizer: valid options=[sgd, adam]')
  cmd.add_argument('--train_path', required=True, help='the path to the training file.')
  cmd.add_argument('--valid_path', required=True, help='the path to the validation file.')
  cmd.add_argument('--test_path', required=False, help='the path to the testing file.')
  cmd.add_argument('--lexicon', required=True, help='the path to the hdf5 file.')

  cmd.add_argument('--gold_valid_path', type=str, help='the path to the validation file.')
  cmd.add_argument('--gold_test_path', type=str, help='the path to the testing file.')

  cmd.add_argument("--model", required=True, help="path to save model")
  cmd.add_argument("--batch_size", "--batch", type=int, default=32, help='the batch size.')
  cmd.add_argument("--hidden_dim", "--hidden", type=int, default=128, help='the hidden dimension.')
  cmd.add_argument("--max_epoch", type=int, default=100, help='the maximum number of iteration.')
  cmd.add_argument("--dropout", type=float, default=0.0, help='the dropout rate')
  cmd.add_argument("--depth", type=int, default=2, help='the depth of lstm')

  cmd.add_argument("--eval_steps", type=int, help='eval every x batches')
  cmd.add_argument("--l2", type=float, default=0.00001, help='the l2 decay rate.')
  cmd.add_argument("--lr", type=float, default=0.01, help='the learning rate.')
  cmd.add_argument("--lr_decay", type=float, default=0, help='the learning rate decay.')
  cmd.add_argument("--clip_grad", type=float, default=1, help='the tense of clipped grad.')
  cmd.add_argument("--consider_word_piece", default=False, action='store_true', help='use word piece.')
  cmd.add_argument('--output', help='The path to the output file.')
  cmd.add_argument("--script", required=True, help="The path to the evaluation script")

  opt = cmd.parse_args(sys.argv[2:])

  print(opt)
  torch.manual_seed(opt.seed)
  random.seed(opt.seed)
  if opt.gpu >= 0:
    torch.cuda.set_device(opt.gpu)
    if opt.seed > 0:
      torch.cuda.manual_seed(opt.seed)

  if opt.gold_valid_path is None:
    opt.gold_valid_path = opt.valid_path

  if opt.gold_test_path is None and opt.test_path is not None:
    opt.gold_test_path = opt.test_path

  use_cuda = opt.gpu >= 0 and torch.cuda.is_available()

  lexicon = h5py.File(opt.lexicon, 'r')
  dim, n_layers = lexicon['#info'][0].item(), lexicon['#info'][1].item()
  logging.info('dim: {}'.format(dim))
  logging.info('n_layers: {}'.format(n_layers))

  raw_training_data, raw_training_labels = read_corpus(opt.train_path)
  raw_valid_data, raw_valid_labels = read_corpus(opt.valid_path)
  if opt.test_path is not None:
    raw_test_data, raw_test_labels = read_corpus(opt.test_path)
  else:
    raw_test_data, raw_test_labels = [], []

  logging.info('training instance: {}, validation instance: {}, test instance: {}.'.format(
    len(raw_training_labels), len(raw_valid_labels), len(raw_test_labels)))
  logging.info('training tokens: {}, validation tokens: {}, test tokens: {}.'.format(
    sum([len(seq) for seq in raw_training_labels]),
    sum([len(seq) for seq in raw_valid_labels]),
    sum([len(seq) for seq in raw_test_labels])))

  if not opt.consider_word_piece:
    label_to_ix = {'<pad>': 0}
  else:
    label_to_ix = {'<pad>': 0, '-word-piece-': 1}
  label_to_index(raw_training_labels, label_to_ix)
  label_to_index(raw_valid_labels, label_to_ix, incremental=False)
  label_to_index(raw_test_labels, label_to_ix, incremental=False)

  logging.info('number of tags: {0}'.format(len(label_to_ix)))

  n_classes = len(label_to_ix)
  ix2label = {ix: label for label, ix in label_to_ix.items()}

  train_payload = create_batches(dim, n_layers, raw_training_data, raw_training_labels,
                                 lexicon, opt.batch_size, use_cuda=use_cuda)

  if opt.eval_steps is None or opt.eval_steps > len(train_payload[0]):
    opt.eval_steps = len(train_payload[0])

  valid_payload = create_batches(dim, n_layers, raw_valid_data, raw_valid_labels,
                                 lexicon, opt.batch_size, shuffle=False, sort=True, keep_full=True,
                                 use_cuda=use_cuda)

  if opt.test_path is not None:
    test_payload = create_batches(dim, n_layers, raw_test_data, raw_test_labels,
                                  lexicon, opt.batch_size, shuffle=False, sort=True, keep_full=True,
                                  use_cuda=use_cuda)
  else:
    test_payload = None

  model = Model(opt, dim, n_layers, n_classes, opt.consider_word_piece, use_cuda)
  logging.info(str(model))
  if use_cuda:
    model = model.cuda()

  need_grad = lambda x: x.requires_grad
  if opt.optimizer.lower() == 'adam':
    optimizer = torch.optim.Adam(filter(need_grad, model.parameters()), lr=opt.lr)
  else:
    optimizer = torch.optim.SGD(filter(need_grad, model.parameters()), lr=opt.lr)

  try:
    os.makedirs(opt.model)
  except OSError as exception:
    if exception.errno != errno.EEXIST:
      raise

  json.dump(vars(opt), codecs.open(os.path.join(opt.model, 'config.json'), 'w', encoding='utf-8'))
  best_valid, test_result = -1e8, -1e8
  for epoch in range(opt.max_epoch):
    best_valid, test_result = train_model(epoch, model, optimizer,
                                          train_payload, valid_payload, test_payload, ix2label, best_valid, test_result)
    if opt.lr_decay > 0:
      optimizer.param_groups[0]['lr'] *= opt.lr_decay
    logging.info('Total encoder time: {:.2f}s'.format(model.eval_time / (epoch + 1)))
    logging.info('Total embedding time: {:.2f}s'.format(model.emb_time / (epoch + 1)))
    logging.info('Total classify time: {:.2f}s'.format(model.classify_time / (epoch + 1)))

  weights = model.weights
  if use_cuda:
    weights = weights.cpu()
  logging.info("weights: {}".format(weights.data.numpy()))
  logging.info("best_valid_acc: {:.6f}".format(best_valid))
  logging.info("test_acc: {:.6f}".format(test_result))


def test():
  cmd = argparse.ArgumentParser('The testing components of')
  cmd.add_argument('--gpu', default=-1, type=int, help='use id of gpu, -1 if cpu.')
  cmd.add_argument("--input", help="the path to the test file.")
  cmd.add_argument('--output', help='the path to the output file.')
  cmd.add_argument("--model", required=True, help="path to save model")
  cmd.add_argument('--lexicon', required=True, help='the path to the hdf5 file.')

  args = cmd.parse_args(sys.argv[2:])

  if args.gpu >= 0:
    torch.cuda.set_device(args.gpu)

  lexicon = h5py.File(args.lexicon, 'r')
  dim, n_layers = lexicon['#info'][0].item(), lexicon['#info'][1].item()
  logging.info('dim: {}'.format(dim))
  logging.info('n_layers: {}'.format(n_layers))

  model_path = args.model

  args2 = dict2namedtuple(json.load(codecs.open(os.path.join(model_path, 'config.json'), 'r', encoding='utf-8')))

  label2id, id2label = {}, {}
  with codecs.open(os.path.join(model_path, 'label.dic'), 'r', encoding='utf-8') as fpi:
    for line in fpi:
      token, i = line.strip().split('\t')
      label2id[token] = int(i)
      id2label[int(i)] = token
  logging.info('number of labels: {0}'.format(len(label2id)))

  use_cuda = args.gpu >= 0 and torch.cuda.is_available()
  
  model = Model(args2, dim, n_layers, len(label2id), use_cuda)
  if use_cuda:
    model = model.cuda()

  raw_test_data, raw_test_labels = read_corpus(args.input)
  label_to_index(raw_test_labels, label2id, incremental=False)

  test_data, test_labels, test_lens, order = create_batches(dim, n_layers, raw_test_data, raw_test_labels, lexicon,
                                                            shuffle=False, sort=True, keep_full=True,
                                                            use_cuda=use_cuda)

  if args.output is not None:
    fpo = codecs.open(args.output, 'w', encoding='utf-8')
  else:
    fpo = codecs.getwriter('utf-8')(sys.stdout)

  model.eval()
  tagset = []
  for x, y, lens in zip(test_data, test_labels, test_lens):
    output, loss = model.forward(x, y)
    output_data = output.data
    for bid in range(len(x)):
      tags = []
      for k in range(lens[bid]):
        tag = id2label[int(output_data[bid][k])]
        tags.append(tag)
      tagset.append(tags)

  for l in order:
    for tag in tagset[l]:
      print(tag, file=fpo)
    print(file=fpo)
  fpo.close()


if __name__ == "__main__":
  if len(sys.argv) > 1 and sys.argv[1] == 'train':
    train()
  elif len(sys.argv) > 1 and sys.argv[1] == 'test':
    test()
  else:
    print('Usage: {0} [train|test] [options]'.format(sys.argv[0]), file=sys.stderr)
