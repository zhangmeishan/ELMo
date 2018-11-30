#!/usr/bin/env python
from __future__ import print_function
from __future__ import unicode_literals
import os
import sys
import codecs
import argparse
import random
import logging
import json
import torch
from torch.autograd import Variable
from modules.elmo import ElmobiLm
from modules.lstm import LstmbiLm
from modules.bengio03 import Bengio03biLm
from modules.lbl import LBLbiLm
from modules.token_embedder import ConvTokenEmbedder, LstmTokenEmbedder
from modules.embedding_layer import EmbeddingLayer
import numpy as np
import h5py
import collections

logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s: %(message)s')


def dict2namedtuple(dic):
  return collections.namedtuple('Namespace', dic.keys())(**dic)


def read_corpus(path, max_chars=None):
  """
  read raw text file. The format of the input is like, one sentence per line
  words are separated by '\t'

  :param path:
  :param max_chars: int, the number of maximum characters in a word, this
    parameter is used when the model is configured with CNN word encoder.
  :return:
  """
  dataset = []
  textset = []
  with codecs.open(path, 'r', encoding='utf-8') as fin:
    for line in fin.read().strip().split('\n'):
      data = ['<bos>']
      text = []
      for token in line.split('\t'):
        text.append(token)
        if max_chars is not None and len(token) + 2 > max_chars:
          token = token[:max_chars - 2]
        data.append(token)
      data.append('<eos>')
      dataset.append(data)
      textset.append(text)
  return dataset, textset


def read_conll_corpus(path, max_chars=None):
  """
  read text in CoNLL-U format.

  :param path:
  :param max_chars:
  :return:
  """
  dataset = []
  textset = []
  with codecs.open(path, 'r', encoding='utf-8') as fin:
    for payload in fin.read().strip().split('\n\n'):
      data = ['<bos>']
      text = []
      lines = payload.splitlines()
      body = [line for line in lines if not line.startswith('#')]
      for line in body:
        fields = line.split('\t')
        num, token = fields[0], fields[1]
        if '-' in num or '.' in num:
          continue
        text.append(token)
        if max_chars is not None and len(token) + 2 > max_chars:
          token = token[:max_chars - 2]
        data.append(token)
      data.append('<eos>')
      dataset.append(data)
      textset.append(text)
  return dataset, textset


def read_conll_char_corpus(path, max_chars=None):
  """

  :param path:
  :param max_chars:
  :return:
  """
  dataset = []
  textset = []
  with codecs.open(path, 'r', encoding='utf-8') as fin:
    for payload in fin.read().strip().split('\n\n'):
      data = ['<bos>']
      text = []
      lines = payload.splitlines()
      body = [line for line in lines if not line.startswith('#')]
      for line in body:
        fields = line.split('\t')
        num, token = fields[0], fields[1]
        if '-' in num or '.' in num:
          continue
        for ch in token:
          text.append(ch)
          if max_chars is not None and len(ch) + 2 > max_chars:
            ch = ch[:max_chars - 2]
          data.append(ch)
      data.append('<eos>')
      dataset.append(data)
      textset.append(text)
  return dataset, textset


def read_conll_char_vi_corpus(path, max_chars=None):
  """

  :param path:
  :param max_chars:
  :return:
  """
  dataset = []
  textset = []
  with codecs.open(path, 'r', encoding='utf-8') as fin:
    for payload in fin.read().strip().split('\n\n'):
      data = ['<bos>']
      text = []
      lines = payload.splitlines()
      body = [line for line in lines if not line.startswith('#')]
      for line in body:
        fields = line.split('\t')
        num, token = fields[0], fields[1]
        if '-' in num or '.' in num:
          continue
        for ch in token.split():
          text.append(ch)
          if max_chars is not None and len(ch) + 2 > max_chars:
            ch = ch[:max_chars - 2]
          data.append(ch)
      data.append('<eos>')
      dataset.append(data)
      textset.append(text)
  return dataset, textset


def create_one_batch(x, word2id, char2id, config, oov='<oov>', pad='<pad>', sort=True, use_cuda=False):
  batch_size = len(x)
  lst = list(range(batch_size))
  if sort:
    lst.sort(key=lambda l: -len(x[l]))

  x = [x[i] for i in lst]
  lens = [len(x[i]) for i in lst]
  max_len = max(lens)

  if word2id is not None:
    oov_id, pad_id = word2id.get(oov, None), word2id.get(pad, None)
    assert oov_id is not None and pad_id is not None
    batch_w = torch.LongTensor(batch_size, max_len).fill_(pad_id)
    for i, x_i in enumerate(x):
      for j, x_ij in enumerate(x_i):
        batch_w[i][j] = word2id.get(x_ij, oov_id)
  else:
    batch_w = None

  if char2id is not None:
    bow_id, eow_id, oov_id, pad_id = char2id.get('<eow>', None), char2id.get('<bow>', None), char2id.get(oov, None), char2id.get(pad, None)

    assert bow_id is not None and eow_id != None and oov_id is not None and pad_id is not None

    if config['token_embedder']['name'].lower() == 'cnn':
      max_chars = config['token_embedder']['max_characters_per_token']
      assert max([len(w) for i in lst for w in x[i]]) + 2 <= max_chars
    elif config['token_embedder']['name'].lower() == 'lstm':
      max_chars = max([len(w) for i in lst for w in x[i]]) + 2  # counting the <bow> and <eow>

    batch_c = torch.LongTensor(batch_size, max_len, max_chars).fill_(pad_id)

    for i, x_i in enumerate(x):
      for j, x_ij in enumerate(x_i):
        batch_c[i][j][0] = bow_id
        if x_ij == '<bos>' or x_ij == '<eos>':
          batch_c[i][j][1] = char2id.get(x_ij)
          batch_c[i][j][2] = eow_id
        else:
          for k, c in enumerate(x_ij):
            batch_c[i][j][k + 1] = char2id.get(c, oov_id)
          batch_c[i][j][len(x_ij) + 1] = eow_id
  else:
    batch_c = None

  masks = [torch.LongTensor(batch_size, max_len).fill_(0), [], []]

  for i, x_i in enumerate(x):
    for j in range(len(x_i)):
      masks[0][i][j] = 1
      if j + 1 < len(x_i):
        masks[1].append(i * max_len + j)
      if j > 0:
        masks[2].append(i * max_len + j)

  assert len(masks[1]) <= batch_size * max_len
  assert len(masks[2]) <= batch_size * max_len

  masks[1] = torch.LongTensor(masks[1])
  masks[2] = torch.LongTensor(masks[2])

  return batch_w, batch_c, lens, masks


# shuffle training examples and create mini-batches
def create_batches(x, batch_size, word2id, char2id, config, perm=None, shuffle=True, sort=True, use_cuda=False, text=None):
  lst = perm or list(range(len(x)))
  if shuffle:
    random.shuffle(lst)

  if sort:
    lst.sort(key=lambda l: -len(x[l]))

  x = [x[i] for i in lst]
  if text is not None:
    text = [text[i] for i in lst]

  sum_len = 0.0
  batches_w, batches_c, batches_lens, batches_masks, batches_text = [], [], [], [], []
  size = batch_size
  nbatch = (len(x) - 1) // size + 1
  for i in range(nbatch):
    start_id, end_id = i * size, (i + 1) * size
    bw, bc, blens, bmasks = create_one_batch(x[start_id: end_id], word2id, char2id, config,
                                             sort=sort, use_cuda=use_cuda)
    sum_len += sum(blens)
    batches_w.append(bw)
    batches_c.append(bc)
    batches_lens.append(blens)
    batches_masks.append(bmasks)
    if text is not None:
      batches_text.append(text[start_id: end_id])

  if sort:
    perm = list(range(nbatch))
    random.shuffle(perm)
    batches_w = [batches_w[i] for i in perm]
    batches_c = [batches_c[i] for i in perm]
    batches_lens = [batches_lens[i] for i in perm]
    batches_masks = [batches_masks[i] for i in perm]
    if text is not None:
      batches_text = [batches_text[i] for i in perm]

  logging.info("{} batches, avg len: {:.1f}".format(nbatch, sum_len / len(x)))
  if text is not None:
    return batches_w, batches_c, batches_lens, batches_masks, batches_text
  return batches_w, batches_c, batches_lens, batches_masks


class Model(torch.nn.Module):
  def __init__(self, config, word_emb_layer, char_emb_layer, use_cuda=False):
    super(Model, self).__init__()
    self.use_cuda = use_cuda
    self.config = config

    if config['token_embedder']['name'].lower() == 'cnn':
      self.token_embedder = ConvTokenEmbedder(config, word_emb_layer, char_emb_layer, use_cuda)
    elif config['token_embedder']['name'].lower() == 'lstm':
      self.token_embedder = LstmTokenEmbedder(config, word_emb_layer, char_emb_layer, use_cuda)

    encoder_name = config['encoder']['name'].lower()
    if encoder_name == 'elmo':
      self.encoder = ElmobiLm(config, use_cuda)
    elif encoder_name == 'lstm':
      self.encoder = LstmbiLm(config, use_cuda)
    elif encoder_name == 'bengio03':
      self.encoder = Bengio03biLm(config, use_cuda)
    elif encoder_name == 'lbl':
      self.encoder = LBLbiLm(config, use_cuda)
    else:
      raise ValueError('Unknown encoder name: {}'.format(encoder_name))

    self.output_dim = config['encoder']['projection_dim']

  def forward(self, word_inp, chars_package, mask_package):
    token_embedding = self.token_embedder(word_inp, chars_package, (mask_package[0].size(0), mask_package[0].size(1)))
    encoder_name = self.config['encoder']['name'].lower()
    if encoder_name == 'elmo':
      mask = Variable(mask_package[0]).cuda() if self.use_cuda else Variable(mask_package[0])
      encoder_output = self.encoder(token_embedding, mask)
      sz = encoder_output.size()
      token_embedding = torch.cat([token_embedding, token_embedding], dim=2).view(1, sz[1], sz[2], sz[3])
      encoder_output = torch.cat([token_embedding, encoder_output], dim=0)
    elif encoder_name == 'lstm':
      encoder_output = self.encoder(token_embedding)
    elif encoder_name == 'bengio03':
      encoder_output = self.encoder(token_embedding)[0]
      sz = encoder_output.size()
      token_embedding = torch.cat([token_embedding, token_embedding], dim=2).view(1, sz[0], sz[1], sz[2])
      encoder_output = encoder_output.view(1, sz[0], sz[1], sz[2])
      encoder_output = torch.cat([token_embedding, encoder_output], dim=0)
    elif encoder_name == 'lbl':
      encoder_output = self.encoder(token_embedding)[0]
      sz = encoder_output.size()
      token_embedding = torch.cat([token_embedding, token_embedding], dim=2).view(1, sz[0], sz[1], sz[2])
      encoder_output = encoder_output.view(1, sz[0], sz[1], sz[2])
      encoder_output = torch.cat([token_embedding, encoder_output], dim=0)
    else:
      raise ValueError('unknown encoder name: {}'.format(encoder_name))

    return encoder_output

  def load_model(self, path):
    self.token_embedder.load_state_dict(torch.load(os.path.join(path, 'token_embedder.pkl'),
                                                   map_location=lambda storage, loc: storage))
    self.encoder.load_state_dict(torch.load(os.path.join(path, 'encoder.pkl'),
                                            map_location=lambda storage, loc: storage))


def test_main():
  # Configurations
  cmd = argparse.ArgumentParser('The testing components of')
  cmd.add_argument('--gpu', default=-1, type=int, help='use id of gpu, -1 if cpu.')
  cmd.add_argument('--input_format', default='plain', choices=('plain', 'conll', 'conll_char', 'conll_char_vi'),
                   help='the input format.')
  cmd.add_argument("--input", help="the path to the raw text file.")
  cmd.add_argument("--output_format", default='hdf5', help='the output format. Supported format includes (hdf5, txt).'
                                                           ' Use comma to separate the format identifiers,'
                                                           ' like \'--output_format=hdf5,plain\'')
  cmd.add_argument("--output_prefix", help='the prefix of the output file. The output file is in the format of '
                                           '<output_prefix>.<output_layer>.<output_format>')
  cmd.add_argument("--output_layer", help='the target layer to output. 0 for the word encoder, 1 for the first LSTM '
                                          'hidden layer, 2 for the second LSTM hidden layer, -1 for an average'
                                          'of 3 layers.')
  cmd.add_argument("--model", required=True, help="path to save model")
  cmd.add_argument("--batch_size", "--batch", type=int, default=1, help='the batch size.')
  args = cmd.parse_args(sys.argv[2:])

  if args.gpu >= 0:
    torch.cuda.set_device(args.gpu)
  use_cuda = args.gpu >= 0 and torch.cuda.is_available()
  # load the model configurations
  args2 = dict2namedtuple(json.load(codecs.open(os.path.join(args.model, 'config.json'), 'r', encoding='utf-8')))

  with open(args2.config_path, 'r') as fin:
    config = json.load(fin)

  # For the model trained with character-based word encoder.
  if config['token_embedder']['char_dim'] > 0:
    char_lexicon = {}
    with codecs.open(os.path.join(args.model, 'char.dic'), 'r', encoding='utf-8') as fpi:
      for line in fpi:
        tokens = line.strip().split('\t')
        if len(tokens) == 1:
          tokens.insert(0, '\u3000')
        token, i = tokens
        char_lexicon[token] = int(i)
    char_emb_layer = EmbeddingLayer(config['token_embedder']['char_dim'], char_lexicon, fix_emb=False, embs=None)
    logging.info('char embedding size: ' + str(len(char_emb_layer.word2id)))
  else:
    char_lexicon = None
    char_emb_layer = None

  # For the model trained with word form word encoder.
  if config['token_embedder']['word_dim'] > 0:
    word_lexicon = {}
    with codecs.open(os.path.join(args.model, 'word.dic'), 'r', encoding='utf-8') as fpi:
      for line in fpi:
        tokens = line.strip().split('\t')
        if len(tokens) == 1:
          tokens.insert(0, '\u3000')
        token, i = tokens
        word_lexicon[token] = int(i)
    word_emb_layer = EmbeddingLayer(config['token_embedder']['word_dim'], word_lexicon, fix_emb=False, embs=None)
    logging.info('word embedding size: ' + str(len(word_emb_layer.word2id)))
  else:
    word_lexicon = None
    word_emb_layer = None

  # instantiate the model
  model = Model(config, word_emb_layer, char_emb_layer, use_cuda)

  if use_cuda:
    model.cuda()

  logging.info(str(model))
  model.load_model(args.model)

  # read test data according to input format
  read_function = read_corpus if args.input_format == 'plain' else (
    read_conll_corpus if args.input_format == 'conll' else (
      read_conll_char_corpus if args.input_format == 'conll_char' else read_conll_char_vi_corpus))

  if config['token_embedder']['name'].lower() == 'cnn':
    test, text = read_function(args.input, config['token_embedder']['max_characters_per_token'])
  else:
    test, text = read_function(args.input)

  # create test batches from the input data.
  test_w, test_c, test_lens, test_masks, test_text = create_batches(
    test, args.batch_size, word_lexicon, char_lexicon, config, use_cuda=use_cuda, text=text)

  # configure the model to evaluation mode.
  model.eval()

  sent_set = set()
  cnt = 0

  output_formats = args.output_format.split(',')
  output_layers = map(int, args.output_layer.split(','))

  handlers = {}
  for output_format in output_formats:
    if output_format not in ('hdf5', 'txt'):
      print('Unknown output_format: {0}'.format(output_format))
      continue
    for output_layer in output_layers:
      filename = '{0}.ly{1}.{2}'.format(args.output_prefix, output_layer, output_format)
      handlers[output_format, output_layer] = h5py.File(filename, 'w') if output_format == 'hdf5' else open(filename, 'w')

      if output_format == 'hdf5':
        info = np.ndarray([config['encoder']['projection_dim'], config['encoder']['n_layers']])
        fout.create_dataset('#info', info.shape, dtype='int', data=info)
      else:
        print('#projection_dim: {}'.format(config['encoder']['projection_dim']), file=fout)
        print('#n_layers: {}'.format(config['encoder']['n_layers']), file=fout)

  for w, c, lens, masks, texts in zip(test_w, test_c, test_lens, test_masks, test_text):
    output = model.forward(w, c, masks)
    for i, text in enumerate(texts):
      sent = '\t'.join(text)
      sent = sent.replace('.', '$period$')
      sent = sent.replace('/', '$backslash$')
      if sent in sent_set:
        continue
      sent_set.add(sent)
      encoder_name = config['encoder']['name'].lower()
      if encoder_name == 'lstm':
        data = output[i, 1:lens[i]-1, :].data
        if use_cuda:
          data = data.cpu()
      elif encoder_name == 'elmo':
        data = output[:, i, 1:lens[i]-1, :].data
        if use_cuda:
          data = data.cpu()
      elif encoder_name == 'bengio03':
        data = output[:, i, 1:lens[i] - 1, :].data
        if use_cuda:
          data = data.cpu()
      elif encoder_name == 'lbl':
        data = output[:, i, 1:lens[i] - 1, :].data
        if use_cuda:
          data = data.cpu()
      else:
        raise ValueError('unknown encoder name: {}'.format(encoder_name))
      data = data.numpy()

      for (output_format, output_layer) in handlers:
        fout = handlers[output_format, output_layer]
        if output_layer == -1:
          payload = np.average(data, axis=0)
        elif output_layer == -2:
          payload = data
        else:
          payload = data[output_layer]

        if output_format == 'hdf5':
          fout.create_dataset(sent, payload.shape, dtype='float32', data=payload)
        else:
          for word, row in zip(text, payload):
            print('{0}\t{1}'.format(word, '\t'.join(['{0:.8f}'.format(elem) for elem in row])), file=fout)
          print('', file=fout)

      cnt += 1
      if cnt % 1000 == 0:
        logging.info('Finished {0} sentences.'.format(cnt))
  for _, handler in handlers.items():
    handler.close()


if __name__ == "__main__":
  if len(sys.argv) > 1 and sys.argv[1] == 'test':
    test_main()
  else:
    print('Usage: {0} [test] [options]'.format(sys.argv[0]), file=sys.stderr)
