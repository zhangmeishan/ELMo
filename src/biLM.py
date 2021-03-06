#!/usr/bin/env python
from __future__ import print_function
from __future__ import unicode_literals
import os
import errno
import sys
import codecs
import argparse
import time
import random
import logging
import json
import torch
import collections
import shutil
from bilm.elmo import ElmobiLm
from bilm.lstm import LstmbiLm
from bilm.bengio03 import Bengio03HighwayBiLm, Bengio03ResNetBiLm
from bilm.lbl import LBLHighwayBiLm, LBLResNetBiLm
from bilm.self_attn import SelfAttentiveLBLBiLM
from bilm.token_embedder import ConvTokenEmbedder, LstmTokenEmbedder
from bilm.batch import Batcher, create_one_batch
from modules.embedding_layer import EmbeddingLayer
from modules.softmax_layer import SoftmaxLayer
from modules.sampled_softmax_layer import SampledSoftmaxLayer
from modules.window_sampled_softmax_layer import WindowSampledSoftmaxLayer
from modules.window_sampled_cnn_softmax_layer import WindowSampledCNNSoftmaxLayer
from dataloader import load_embedding
from collections import Counter
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)-15s %(levelname)s: %(message)s')


def dict2namedtuple(dic):
  return collections.namedtuple('Namespace', dic.keys())(**dic)


def split_train_and_valid(data, valid_size):
  valid_size = min(valid_size, len(data) // 10)
  random.shuffle(data)
  return data[valid_size:], data[:valid_size]


def count_tokens(raw_data):
  return sum([len(s) - 1 for s in raw_data])


def break_sentence(sentence, max_sent_len):
  """
  For example, for a sentence with 70 words, supposing the the `max_sent_len'
  is 30, break it into 3 sentences.

  :param sentence: list[str] the sentence
  :param max_sent_len:
  :return:
  """
  ret = []
  cur = 0
  l = len(sentence)
  while cur < l:
    if cur + max_sent_len + 5 >= l:
      ret.append(sentence[cur: l])
      break
    ret.append(sentence[cur: min(l, cur + max_sent_len)])
    cur += max_sent_len
  return ret


def read_corpus(path, max_chars=None, max_sent_len=20):
  """
  read raw text file
  :param path: str
  :param max_chars: int
  :param max_sent_len: int
  :return:
  """
  data = []
  with codecs.open(path, 'r', encoding='utf-8') as fin:
    for line in fin:
      data.append('<bos>')
      for token in line.strip().split():
        if max_chars is not None and len(token) + 2 > max_chars:
          token = token[:max_chars - 2]
        data.append(token)
      data.append('<eos>')
  dataset = break_sentence(data, max_sent_len)
  return dataset


class Model(torch.nn.Module):
  def __init__(self, config, word_emb_layer, char_emb_layer, n_class, use_cuda=False):
    super(Model, self).__init__() 
    self.use_cuda = use_cuda
    self.config = config
    self.dropout = torch.nn.Dropout(p=config['dropout'])

    token_embedder_name = config['token_embedder']['name'].lower()
    if token_embedder_name == 'cnn':
      self.token_embedder = ConvTokenEmbedder(config, word_emb_layer, char_emb_layer, use_cuda)
    elif token_embedder_name == 'lstm':
      self.token_embedder = LstmTokenEmbedder(config, word_emb_layer, char_emb_layer, use_cuda)
    else:
      raise ValueError('Unknown token embedder name: {}'.format(token_embedder_name))

    encoder_name = config['encoder']['name'].lower()
    if encoder_name == 'elmo':
      self.encoder = ElmobiLm(config, use_cuda)
    elif encoder_name == 'lstm':
      self.encoder = LstmbiLm(config, use_cuda)
    elif encoder_name == 'bengio03highway':
      self.encoder = Bengio03HighwayBiLm(config, use_cuda)
    elif encoder_name == 'bengio03resnet':
      self.encoder = Bengio03ResNetBiLm(config, use_cuda)
    elif encoder_name == 'lblhighway':
      self.encoder = LBLHighwayBiLm(config, use_cuda)
    elif encoder_name == 'lblresnet':
      self.encoder = LBLResNetBiLm(config, use_cuda)
    elif encoder_name == 'selfattn':
      self.encoder = SelfAttentiveLBLBiLM(config, use_cuda)
    else:
      raise ValueError('Unknown encoder name: {}'.format(encoder_name))

    self.output_dim = config['encoder']['projection_dim']
    classify_layer_name = config['classifier']['name'].lower()
    if classify_layer_name == 'softmax':
      self.classify_layer = SoftmaxLayer(self.output_dim, n_class)
    elif classify_layer_name == 'cnn_softmax':
      self.classify_layer = WindowSampledCNNSoftmaxLayer(self.token_embedder, self.output_dim, n_class,
                                                         config['classifier']['n_samples'], config['classifier']['corr_dim'],
                                                         use_cuda)
    elif classify_layer_name == 'sampled_softmax':
      self.classify_layer = SampledSoftmaxLayer(self.output_dim, n_class, config['classifier']['n_samples'],
                                                use_cuda, unk_id=0)
    elif classify_layer_name == 'window_sampled_softmax':
      self.classify_layer = WindowSampledSoftmaxLayer(self.output_dim, n_class, config['classifier']['n_samples'],
                                                      use_cuda)
    else:
      raise ValueError('Unknown classify_layer: {}'.format(classify_layer_name))

  def forward(self, word_inp, chars_inp, mask_package):
    """

    :param word_inp:
    :param chars_inp:
    :param mask_package: Tuple[]
    :return:
    """
    classifier_name = self.config['classifier']['name'].lower()

    if self.training and classifier_name in ('cnn_softmax', 'window_sampled_softmax'):
      self.classify_layer.update_negative_samples(word_inp, chars_inp, mask_package[0])
      self.classify_layer.update_embedding_matrix()

    token_embedding = self.token_embedder(word_inp, chars_inp, (mask_package[0].size(0), mask_package[0].size(1)))
    token_embedding = self.dropout(token_embedding)

    encoder_name = self.config['encoder']['name'].lower()
    if encoder_name == 'elmo':
      mask = torch.autograd.Variable(mask_package[0]).cuda() if self.use_cuda else \
        torch.autograd.Variable(mask_package[0])
      encoder_output = self.encoder(token_embedding, mask)
      n_layers = encoder_output.size()[0]
      encoder_output = encoder_output[n_layers - 1]
      # [batch_size, len, hidden_size]
    elif encoder_name == 'lstm':
      encoder_output = self.encoder(token_embedding)
    elif encoder_name in ('bengio03highway', 'bengio03resnet', 'lblhighway', 'lblresnet', 'selfattn'):
      encoder_output = self.encoder(token_embedding)
      n_layers = encoder_output.size()[0]
      encoder_output = encoder_output[n_layers - 1]
    else:
      raise ValueError('Unknown encoder name: {}'.format(encoder_name))

    encoder_output = self.dropout(encoder_output)
    forward, backward = encoder_output.split(self.output_dim, 2)

    word_inp = torch.autograd.Variable(word_inp)
    mask1 = torch.autograd.Variable(mask_package[1], requires_grad=False)
    mask2 = torch.autograd.Variable(mask_package[2], requires_grad=False)

    if self.use_cuda:
      word_inp = word_inp.cuda()
      mask1 = mask1.cuda()
      mask2 = mask2.cuda()

    forward_x = forward.contiguous().view(-1, self.output_dim).index_select(0, mask1)
    forward_y = word_inp.contiguous().view(-1).index_select(0, mask2)

    backward_x = backward.contiguous().view(-1, self.output_dim).index_select(0, mask2)
    backward_y = word_inp.contiguous().view(-1).index_select(0, mask1)

    return self.classify_layer(forward_x, forward_y), self.classify_layer(backward_x, backward_y)

  def save_model(self, path, save_classify_layer):
    torch.save(self.token_embedder.state_dict(), os.path.join(path, 'token_embedder.pkl'))    
    torch.save(self.encoder.state_dict(), os.path.join(path, 'encoder.pkl'))
    if save_classify_layer:
      torch.save(self.classify_layer.state_dict(), os.path.join(path, 'classifier.pkl'))

  def load_model(self, path):
    self.token_embedder.load_state_dict(torch.load(os.path.join(path, 'token_embedder.pkl')))
    self.encoder.load_state_dict(torch.load(os.path.join(path, 'encoder.pkl')))
    self.classify_layer.load_state_dict(torch.load(os.path.join(path, 'classifier.pkl')))


def eval_model(model, valid_batch):
  model.eval()
  if model.config['classifier']['name'].lower() in ('window_sampled_cnn_softmax', 'window_sampled_softmax'):
    model.classify_layer.update_embedding_matrix()
  total_loss, total_tag = 0.0, 0
  for w, c, lens, masks in valid_batch.get():
    loss_forward, loss_backward = model.forward(w, c, masks)
    total_loss += loss_forward.item()
    n_tags = sum(lens)
    total_tag += n_tags
  model.train()
  return np.exp(total_loss / total_tag)


def train_model(epoch, opt, model, optimizer,
                train_batch, valid_batch, test_batch, best_train, best_valid, test_result):
  """
  Training model for one epoch

  :param epoch:
  :param opt:
  :param model:
  :param optimizer:
  :param train_batch:
  :param best_train:
  :param valid_batch:
  :param best_valid:
  :param test_batch:
  :param test_result:
  :return:
  """
  model.train()

  total_loss, total_tag = 0.0, 0
  cnt = 0
  start_time = time.time()

  for w, c, lens, masks in train_batch.get():
    cnt += 1
    model.zero_grad()
    loss_forward, loss_backward = model.forward(w, c, masks)

    loss = (loss_forward + loss_backward) / 2.0
    total_loss += loss_forward.item()
    n_tags = sum(lens)
    total_tag += n_tags
    loss.backward()

    torch.nn.utils.clip_grad_norm_(model.parameters(), opt.clip_grad)
    optimizer.step()
    if cnt * opt.batch_size % 1024 == 0:
      logging.info("Epoch={} iter={} lr={:.6f} train_ppl={:.6f} time={:.2f}s".format(
        epoch, cnt, optimizer.param_groups[0]['lr'],
        np.exp(total_loss / total_tag), time.time() - start_time
      ))
      start_time = time.time()

    if cnt % opt.eval_steps == 0 or cnt % train_batch.num_batches() == 0:
      train_ppl = np.exp(total_loss / total_tag)
      logging.info("Epoch={} iter={} lr={:.6f} train_ppl={:.6f}".format(
        epoch, cnt, optimizer.param_groups[0]['lr'], train_ppl))

      if valid_batch is None:
        if train_ppl < best_train:
          best_train = train_ppl
          logging.info("New record achieved on training dataset!")
          model.save_model(opt.model, opt.save_classify_layer)      
      else:
        valid_ppl = eval_model(model, valid_batch)
        logging.info("Epoch={} iter={} lr={:.6f} valid_ppl={:.6f}".format(
          epoch, cnt, optimizer.param_groups[0]['lr'], valid_ppl))

        if valid_ppl < best_valid:
          model.save_model(opt.model, opt.save_classify_layer)
          best_valid = valid_ppl
          logging.info("New record achieved!")

          if test is not None:
            test_result = eval_model(model, test_batch)
            logging.info("Epoch={} iter={} lr={:.6f} test_ppl={:.6f}".format(
              epoch, cnt, optimizer.param_groups[0]['lr'], test_result))
  return best_train, best_valid, test_result


def get_truncated_vocab(dataset, min_count):
  """

  :param dataset:
  :param min_count:
  :return:
  """
  word_count = Counter()
  for sentence in dataset:
    word_count.update(sentence)

  word_count = list(word_count.items())
  word_count.sort(key=lambda x: x[1], reverse=True)

  for i, (word, count) in enumerate(word_count):
    if count < min_count:
      break

  logging.info('Truncated word count: {0}.'.format(sum([count for word, count in word_count[i:]])))
  logging.info('Original vocabulary size: {0}.'.format(len(word_count)))
  return word_count[:i]


def train():
  cmd = argparse.ArgumentParser(sys.argv[0], conflict_handler='resolve')
  cmd.add_argument('--seed', default=1, type=int, help='The random seed.')
  cmd.add_argument('--gpu', default=-1, type=int, help='Use id of gpu, -1 if cpu.')

  cmd.add_argument('--train_path', required=True, help='The path to the training file.')
  cmd.add_argument('--valid_path', help='The path to the development file.')
  cmd.add_argument('--test_path', help='The path to the testing file.')

  cmd.add_argument('--config_path', required=True, help='the path to the config file.')
  cmd.add_argument("--word_embedding", help="The path to word vectors.")

  cmd.add_argument('--optimizer', default='sgd', choices=['sgd', 'adam', 'adagrad'],
                   help='the type of optimizer: valid options=[sgd, adam, adagrad]')
  cmd.add_argument("--lr", type=float, default=0.01, help='the learning rate.')
  cmd.add_argument("--lr_decay", type=float, default=0, help='the learning rate decay.')

  cmd.add_argument("--model", required=True, help="path to save model")
  
  cmd.add_argument("--batch_size", "--batch", type=int, default=32, help='the batch size.')
  cmd.add_argument("--max_epoch", type=int, default=100, help='the maximum number of iteration.')
  
  cmd.add_argument("--clip_grad", type=float, default=5, help='the tense of clipped grad.')

  cmd.add_argument('--max_sent_len', type=int, default=20, help='maximum sentence length.')

  cmd.add_argument('--min_count', type=int, default=5, help='minimum word count.')

  cmd.add_argument('--max_vocab_size', type=int, default=150000, help='maximum vocabulary size.')

  cmd.add_argument('--save_classify_layer', default=False, action='store_true',
                   help="whether to save the classify layer")

  cmd.add_argument('--valid_size', type=int, default=0, help="size of validation dataset when there's no valid.")
  cmd.add_argument('--eval_steps', required=False, type=int, help='report every xx batches.')

  opt = cmd.parse_args(sys.argv[2:])

  with open(opt.config_path, 'r') as fin:
    config = json.load(fin)

  # Dump configurations
  print(opt)
  print(config)

  # Set seed.
  torch.manual_seed(opt.seed)
  random.seed(opt.seed)
  np.random.seed(opt.seed)
  if opt.gpu >= 0:
    torch.cuda.set_device(opt.gpu)
    if opt.seed > 0:
      torch.cuda.manual_seed(opt.seed)

  use_cuda = opt.gpu >= 0 and torch.cuda.is_available()

  token_embedder_name = config['token_embedder']['name'].lower()
  token_embedder_max_chars = config['token_embedder'].get('max_characters_per_token', None)

  # Load training data.
  if token_embedder_name == 'cnn':
    raw_training_data = read_corpus(opt.train_path, token_embedder_max_chars, opt.max_sent_len)
  elif token_embedder_name == 'lstm':
    raw_training_data = read_corpus(opt.train_path, max_sent_len=opt.max_sent_len)
  else:
    raise ValueError('Unknown token embedder name: {}'.format(token_embedder_name))
  logging.info('training instance: {}, training tokens: {}.'.format(
    len(raw_training_data), count_tokens(raw_training_data)))

  # Load valid data if path is provided, else use 10% of training data as valid data
  if opt.valid_path is not None:
    if token_embedder_name == 'cnn':
      raw_valid_data = read_corpus(opt.valid_path, token_embedder_max_chars, opt.max_sent_len)
    elif token_embedder_name == 'lstm':
      raw_valid_data = read_corpus(opt.valid_path, max_sent_len=opt.max_sent_len)
    else:
      raise ValueError('Unknown token embedder name: {}'.format(token_embedder_name))
    logging.info('valid instance: {}, valid tokens: {}.'.format(len(raw_valid_data), count_tokens(raw_valid_data)))
  elif opt.valid_size > 0:
    raw_training_data, raw_valid_data = split_train_and_valid(raw_training_data, opt.valid_size)
    logging.info('training instance: {}, training tokens after division: {}.'.format(
      len(raw_training_data), count_tokens(raw_training_data)))
    logging.info('valid instance: {}, valid tokens: {}.'.format(len(raw_valid_data), count_tokens(raw_valid_data)))
  else:
    raw_valid_data = None

  # Load test data if path is provided.
  if opt.test_path is not None:
    if token_embedder_name == 'cnn':
      raw_test_data = read_corpus(opt.test_path, token_embedder_max_chars, opt.max_sent_len)
    elif token_embedder_name == 'lstm':
      raw_test_data = read_corpus(opt.test_path, max_sent_len=opt.max_sent_len)
    else:
      raise ValueError('Unknown token embedder name: {}'.format(token_embedder_name))
    logging.info('testing instance: {}, testing tokens: {}.'.format(len(raw_test_data), count_tokens(raw_test_data)))
  else:
    raw_test_data = None

  # Use pre-trained word embeddings
  if opt.word_embedding is not None:
    embs = load_embedding(opt.word_embedding)
    word_lexicon = {word: i for i, word in enumerate(embs[0])}  
  else:
    embs = None
    word_lexicon = {}

  # Ensure index of '<oov>' is 0
  for special_word in ['<oov>', '<bos>', '<eos>', '<pad>']:
    if special_word not in word_lexicon:
      word_lexicon[special_word] = len(word_lexicon)

  # Maintain the vocabulary. vocabulary is used in either WordEmbeddingInput or softmax classification
  vocab = get_truncated_vocab(raw_training_data, opt.min_count)

  for word, _ in vocab:
    if word not in word_lexicon:
      word_lexicon[word] = len(word_lexicon)

  # Word Embedding
  if config['token_embedder']['word_dim'] > 0:
    word_emb_layer = EmbeddingLayer(config['token_embedder']['word_dim'], word_lexicon, fix_emb=False, embs=embs)
    logging.info('Word embedding size: {0}'.format(len(word_emb_layer.word2id)))
  else:
    word_emb_layer = None
    logging.info('Vocabulary size: {0}'.format(len(word_lexicon)))

  # Character Lexicon
  if config['token_embedder']['char_dim'] > 0:
    char_lexicon = {}
    for sentence in raw_training_data:
      for word in sentence:
        for ch in word:
          if ch not in char_lexicon:
            char_lexicon[ch] = len(char_lexicon)

    for special_char in ['<bos>', '<eos>', '<oov>', '<pad>', '<bow>', '<eow>']:
      if special_char not in char_lexicon:
        char_lexicon[special_char] = len(char_lexicon)

    char_emb_layer = EmbeddingLayer(config['token_embedder']['char_dim'], char_lexicon, fix_emb=False)
    logging.info('Char embedding size: {0}'.format(len(char_emb_layer.word2id)))
  else:
    char_lexicon = None
    char_emb_layer = None

  # Create training batch
  training_data = Batcher(raw_training_data, opt.batch_size, word_lexicon, char_lexicon, config)

  # Set up evaluation steps.
  if opt.eval_steps is None:
    opt.eval_steps = training_data.num_batches()
  logging.info('Evaluate every {0} batches.'.format(opt.eval_steps))

  # If there is valid, create valid batch.
  if raw_valid_data is not None:
    valid_data = Batcher(
      raw_valid_data, opt.batch_size, word_lexicon, char_lexicon, config, sort=False, shuffle=False)
  else:
    valid_data = None

  # If there is test, create test batch.
  if raw_test_data is not None:
    test_data = Batcher(
      raw_test_data, opt.batch_size, word_lexicon, char_lexicon, config, sort=False, shuffle=False)
  else:
    test_data = None

  label_to_ix = word_lexicon
  logging.info('vocab size: {0}'.format(len(label_to_ix)))
  n_classes = len(label_to_ix)

  model = Model(config, word_emb_layer, char_emb_layer, n_classes, use_cuda)

  logging.info(str(model))
  if use_cuda:
    model = model.cuda()

  need_grad = lambda x: x.requires_grad
  if opt.optimizer.lower() == 'adam':
    optimizer = torch.optim.Adam(filter(need_grad, model.parameters()), lr=opt.lr)
  elif opt.optimizer.lower() == 'sgd':
    optimizer = torch.optim.SGD(filter(need_grad, model.parameters()), lr=opt.lr)
  elif opt.optimizer.lower() == 'adagrad':
    optimizer = torch.optim.Adagrad(filter(need_grad, model.parameters()), lr=opt.lr)
  else:
    raise ValueError('Unknown optimizer {}'.format(opt.optimizer.lower()))

  try:
    os.makedirs(opt.model)
  except OSError as exception:
    if exception.errno != errno.EEXIST:
      raise

  if config['token_embedder']['char_dim'] > 0:
    with codecs.open(os.path.join(opt.model, 'char.dic'), 'w', encoding='utf-8') as fpo:
      for ch, i in char_emb_layer.word2id.items():
        print('{0}\t{1}'.format(ch, i), file=fpo)

  with codecs.open(os.path.join(opt.model, 'word.dic'), 'w', encoding='utf-8') as fpo:
    for w, i in word_lexicon.items():
      print('{0}\t{1}'.format(w, i), file=fpo)

  new_config_path = os.path.join(opt.model, os.path.basename(opt.config_path))
  shutil.copy(opt.config_path, new_config_path)
  opt.config_path = new_config_path
  json.dump(vars(opt), codecs.open(os.path.join(opt.model, 'config.json'), 'w', encoding='utf-8'))

  best_train = 1e+8
  best_valid = 1e+8
  test_result = 1e+8

  for epoch in range(opt.max_epoch):
    best_train, best_valid, test_result = train_model(
      epoch, opt, model, optimizer, training_data, valid_data, test_data, best_train, best_valid, test_result)

    if opt.lr_decay > 0:
      optimizer.param_groups[0]['lr'] *= opt.lr_decay

  if raw_valid_data is None:
    logging.info("best train ppl: {:.6f}.".format(best_train))
  elif raw_test_data is None:
    logging.info("best train ppl: {:.6f}, best valid ppl: {:.6f}.".format(best_train, best_valid))
  else:
    logging.info("best train ppl: {:.6f}, best valid ppl: {:.6f}, test ppl: {:.6f}.".format(
      best_train, best_valid, test_result))


def test():
  cmd = argparse.ArgumentParser('The testing components of')
  cmd.add_argument('--gpu', default=-1, type=int, help='use id of gpu, -1 if cpu.')
  cmd.add_argument("--input", help="the path to the raw text file.")
  cmd.add_argument("--model", required=True, help="path to save model")
  cmd.add_argument("--batch_size", "--batch", type=int, default=1, help='the batch size.')
  args = cmd.parse_args(sys.argv[2:])

  if args.gpu >= 0:
    torch.cuda.set_device(args.gpu)
  use_cuda = args.gpu >= 0 and torch.cuda.is_available()
  
  args2 = dict2namedtuple(json.load(codecs.open(os.path.join(args.model, 'config.json'), 'r', encoding='utf-8')))

  with open(args2.config_path, 'r') as fin:
    config = json.load(fin)

  if config['token_embedder']['char_dim'] > 0:
    char_lexicon = {}
    with codecs.open(os.path.join(args.model, 'char.dic'), 'r', encoding='utf-8') as fpi:
      for line in fpi:
        tokens = line.strip().split('\t')
        if len(tokens) == 1:
          tokens.insert(0, '\u3000')
        token, i = tokens
        char_lexicon[token] = int(i)
    char_emb_layer = EmbeddingLayer(config['token_embedder']['char_dim'], char_lexicon, fix_emb=False)
    logging.info('char embedding size: ' + str(len(char_emb_layer.word2id)))
  else:
    char_lexicon = None
    char_emb_layer = None

  word_lexicon = {}
  with codecs.open(os.path.join(args.model, 'word.dic'), 'r', encoding='utf-8') as fpi:
    for line in fpi:
      tokens = line.strip().split('\t')
      if len(tokens) == 1:
        tokens.insert(0, '\u3000')
      token, i = tokens
      word_lexicon[token] = int(i)

  if config['token_embedder']['word_dim'] > 0:
    word_emb_layer = EmbeddingLayer(config['token_embedder']['word_dim'], word_lexicon, fix_emb=False, embs=None)
    logging.info('word embedding size: ' + str(len(word_emb_layer.word2id)))
  else:
    word_emb_layer = None
  
  model = Model(config, word_emb_layer, char_emb_layer, len(word_lexicon), use_cuda)

  if use_cuda:
    model.cuda()

  logging.info(str(model))
  model.load_model(args.model)
  if config['token_embedder']['name'].lower() == 'cnn':
    test = read_corpus(args.input, config['token_embedder']['max_characters_per_token'], max_sent_len=10000)
  elif config['token_embedder']['name'].lower() == 'lstm':
    test = read_corpus(args.input, max_sent_len=10000)
  else:
    raise ValueError('')

  test_batch = Batcher(test, args.batch_size, word_lexicon, char_lexicon, config, sort=False, shuffle=False)

  test_result = eval_model(model, test_batch)

  logging.info("test_ppl={:.6f}".format(test_result))


if __name__ == "__main__":
  if len(sys.argv) > 1 and sys.argv[1] == 'train':
    train()
  elif len(sys.argv) > 1 and sys.argv[1] == 'test':
    test()
  else:
    print('Usage: {0} [train|test] [options]'.format(sys.argv[0]), file=sys.stderr)
