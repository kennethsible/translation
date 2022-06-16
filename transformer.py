import torch, random, copy, math, tqdm, time, re
from torch.optim.lr_scheduler import ReduceLROnPlateau
from sacremoses import MosesTokenizer, MosesDetokenizer
from subword_nmt.apply_bpe import BPE, read_vocabulary
from sacrebleu.metrics import BLEU, CHRF
from torch import nn

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

bleu, chrf = BLEU(), CHRF()
mt, md = MosesTokenizer(lang='de'), MosesDetokenizer(lang='en')
bpe = BPE(open('data/out'), vocab=read_vocabulary(open('data/vocab.de'), 50))

def detokenize(words):
    return re.sub('(@@ )|(@@ ?$)', '', md.detokenize(words))

def clone(module, N):
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])

class Embedding(nn.Module):
    def __init__(self, d_model, vocab):
        super().__init__()
        self.emb = nn.Embedding(vocab, d_model)
        self.d_model = d_model

    def forward(self, x):
        return self.emb(x) * math.sqrt(self.d_model)

class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout, max_len=5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        penc = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * -(math.log(10000.) / d_model)
        )
        penc[:, 0::2] = torch.sin(position * div_term)
        penc[:, 1::2] = torch.cos(position * div_term)
        penc = penc.unsqueeze(0)
        self.register_buffer('penc', penc)

    def forward(self, x):
        x = x + self.penc[:, : x.size(1)].requires_grad_(False)
        return self.dropout(x)

class FeedForward(nn.Module):

    def __init__(self, d_model, d_ff, dropout=0.1):
        super().__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(self.w_1(x).relu()))

class LayerNorm(nn.Module):

    def __init__(self, features, eps=1e-6):
        super().__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = torch.mean(x, -1, keepdim=True)
        std = torch.std(x, -1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2

class LogSoftmax(nn.Module):

    def __init__(self, d_model, vocab):
        super().__init__()
        self.proj = nn.Linear(d_model, vocab)

    def forward(self, x):
        return torch.log_softmax(self.proj(x), dim=-1)

class MultiHeadedAttention(nn.Module):

    def __init__(self, h, d_model, dropout=0.1):
        super().__init__()
        assert d_model % h == 0
        self.d_k = d_model // h # d_v = d_k
        self.h = h
        self.linears = clone(nn.Linear(d_model, d_model), 4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

    @staticmethod
    def attention(query, key, value, mask=None, dropout=None):
        d_k = query.size(-1)
        scores = (query @ key.transpose(-2, -1)) / math.sqrt(d_k)
        if mask is not None:
            scores = scores.masked_fill(mask == 0, -torch.inf) # -1e9
        p_attn = scores.softmax(dim=-1)
        if dropout is not None:
            p_attn = dropout(p_attn)
        return p_attn @ value, p_attn

    def forward(self, query, key, value, mask=None):
        if mask is not None:
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)

        # 1) Do all the linear projections in batch from d_model => h x d_k
        query, key, value = [
            lin(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2)
            for lin, x in zip(self.linears, (query, key, value))
        ]

        # 2) Apply attention on all the projected vectors in batch.
        x, self.attn = self.attention(
            query, key, value, mask=mask, dropout=self.dropout
        )

        # 3) "Concat" using a view and apply a final linear.
        x = (
            x.transpose(1, 2)
            .contiguous()
            .view(nbatches, -1, self.h * self.d_k)
        )
        del query
        del key
        del value
        return self.linears[-1](x)

class SublayerConnection(nn.Module):

    def __init__(self, size, dropout):
        super().__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        return x + self.dropout(sublayer(self.norm(x)))

class EncoderLayer(nn.Module):

    def __init__(self, d_model, d_ff, h, dropout):
        super().__init__()
        self.att = MultiHeadedAttention(h, d_model)
        self.ff = FeedForward(d_model, d_ff, dropout)
        self.sublayers = clone(SublayerConnection(d_model, dropout), 2)

    def forward(self, x, mask):
        x = self.sublayers[0](x, lambda x: self.att(x, x, x, mask))
        return self.sublayers[1](x, self.ff)

class Encoder(nn.Module):

    def __init__(self, d_model, d_ff, h, dropout, N):
        super().__init__()
        self.layers = clone(EncoderLayer(d_model, d_ff, h, dropout), N)
        self.norm = LayerNorm(d_model)

    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)

class DecoderLayer(nn.Module):

    def __init__(self, d_model, d_ff, h, dropout):
        super().__init__()
        self.att1 = MultiHeadedAttention(h, d_model)
        self.att2 = MultiHeadedAttention(h, d_model)
        self.ff = FeedForward(d_model, d_ff, dropout)
        self.sublayers = clone(SublayerConnection(d_model, dropout), 3)

    def forward(self, x, m, src_mask, tgt_mask):
        x = self.sublayers[0](x, lambda x: self.att1(x, x, x, tgt_mask))
        x = self.sublayers[1](x, lambda x: self.att2(x, m, m, src_mask))
        return self.sublayers[2](x, self.ff)

class Decoder(nn.Module):

    def __init__(self, d_model, d_ff, h, dropout, N):
        super().__init__()
        self.layers = clone(DecoderLayer(d_model, d_ff, h, dropout), N)
        self.norm = LayerNorm(d_model)

    def forward(self, x, memory, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, memory, src_mask, tgt_mask)
        return self.norm(x)

class Model(nn.Module):

    def __init__(self, src_vocab, tgt_vocab, d_model=512, d_ff=2048, h=8, dropout=0.1, N=6):
        super().__init__()
        self.encoder = Encoder(d_model, d_ff, h, dropout, N)
        self.decoder = Decoder(d_model, d_ff, h, dropout, N)
        self.src_embed = nn.Sequential(Embedding(d_model, src_vocab), PositionalEncoding(d_model, dropout))
        self.tgt_embed = nn.Sequential(Embedding(d_model, tgt_vocab), PositionalEncoding(d_model, dropout))
        self.generator = LogSoftmax(d_model, tgt_vocab)
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, src, tgt, src_mask, tgt_mask):
        return self.decode(self.encode(src, src_mask), src_mask, tgt, tgt_mask)

    def encode(self, src, src_mask):
        return self.encoder(self.src_embed(src), src_mask)

    def decode(self, memory, src_mask, tgt, tgt_mask):
        return self.decoder(self.tgt_embed(tgt), memory, src_mask, tgt_mask)

def subsequent_mask(size):
    att_shape = (1, size, size)
    subsequent_mask = torch.triu(torch.ones(att_shape), diagonal=1).type(torch.uint8)
    return subsequent_mask == 0

class LossCompute:

    def __init__(self, generator, criterion):
        self.generator = generator
        self.criterion = criterion

    def __call__(self, x, y):
        x = self.generator(x)
        return self.criterion(x.contiguous().view(-1, x.size(-1)), y.contiguous().view(-1))

class Vocab:

    def __init__(self):
        self.num_to_word = ['<BOS>', '<EOS>', '<PAD>', '<UNK>']
        self.word_to_num = {word: i for i, word in enumerate(self.num_to_word)}

    def add(self, word):
        if word not in self.word_to_num:
            num = len(self.num_to_word)
            self.num_to_word.append(word)
            self.word_to_num[word] = num

    def remove(self, word):
        if word in self.word_to_num:
            self.num_to_word.remove(word)
            self.word_to_num.pop(word)

    def numberize(self, *words):
        nums = [self.word_to_num[word] if word in self.word_to_num
            else self.word_to_num['<UNK>'] for word in words]
        return torch.tensor(nums) if len(nums) > 1 else torch.tensor(nums[:1])

    def denumberize(self, *nums):
        words = [self.num_to_word[num] for num in nums]
        return words if len(words) > 1 else words[0]

    def __len__(self):
        return len(self.num_to_word)

class Batch:

    def __init__(self, src, tgt=None, pad=2):
        self.src = src
        self.src_mask = (src != pad).unsqueeze(-2)
        if tgt is not None:
            self.tgt = tgt[:, :-1]
            self.tgt_y = tgt[:, 1:]
            self.tgt_mask = self.create_mask(self.tgt, pad)
            self.ntokens = (self.tgt_y != pad).detach().sum()

    @staticmethod
    def create_mask(tgt, pad):
        tgt_mask = (tgt != pad).unsqueeze(-2)
        tgt_mask = tgt_mask & subsequent_mask(tgt.size(-1)).type_as(tgt_mask.detach())
        return tgt_mask

def train_epoch(data, model, loss_compute, optimizer=None, mode='train'):
    total_loss = 0
    total_tokens = 0
    for batch in data:
        out = model(batch.src, batch.tgt, batch.src_mask, batch.tgt_mask)
        loss = loss_compute(out, batch.tgt_y)
        if mode == 'train':
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        total_loss += loss.item()
        total_tokens += batch.ntokens
        del loss
    return total_loss / total_tokens

def greedy_decode(model, src, src_mask, max_len=64, start_word=0):
    memory = model.encode(src, src_mask)
    tgt = torch.full((src.size(0), 1), start_word).type_as(src)
    for _ in range(max_len - 1):
        out = model.decode(memory, src_mask, tgt, subsequent_mask(tgt.size(-1)).type_as(src))
        prob = model.generator(out[:, -1])
        next_word = torch.argmax(prob, dim=-1)
        tgt = torch.cat([tgt, next_word.view(src.size(0), 1)], dim=-1)
    return tgt

def batch_data(data, batch_size):
    data.sort(key=lambda x: len(x[0]))
    batched = []
    for i in range(batch_size, len(data) + 1, batch_size):
        batch = data[(i - batch_size):i]
        src_max_len = max(len(src_words) for src_words, _ in batch)
        tgt_max_len = max(len(tgt_words) for _, tgt_words in batch)
        for src_words, tgt_words in batch:
            src_res = src_max_len - len(src_words)
            tgt_res = tgt_max_len - len(tgt_words)
            if src_res > 0:
                src_words.extend(src_res * ['<PAD>'])
            if tgt_res > 0:
                tgt_words.extend(tgt_res * ['<PAD>'])
        batched.append(batch)
    return batched

def train_model(max_len, data_limit, batch_size, num_epochs, lr):
    train_data = []
    for line in open('data/train.bpe.de-en'):
        src_line, tgt_line = line.split('\t')
        src_words = ['<BOS>'] + src_line.split() + ['<EOS>']
        tgt_words = ['<BOS>'] + tgt_line.split() + ['<EOS>']
        if max_len is None or len(src_words) <= max_len:
            train_data.append((src_words, tgt_words))

    assert data_limit < len(train_data)
    valid_start = math.ceil(0.995 * data_limit)
    valid_data = batch_data(train_data[valid_start:data_limit], batch_size)
    assert len(valid_data) == (data_limit - valid_start)//batch_size
    train_data = batch_data(train_data[:valid_start], batch_size)
    assert len(train_data) == valid_start//batch_size

    src_vocab, tgt_vocab = Vocab(), Vocab()
    with open('data/vocab.de') as vocab_file:
        for line in vocab_file.readlines():
            src_vocab.add(line.split()[0])
    with open('data/vocab.en') as vocab_file:
        for line in vocab_file.readlines():
            tgt_vocab.add(line.split()[0])

    pad_idx = tgt_vocab.numberize('<PAD>').item()
    model = Model(len(src_vocab), len(tgt_vocab)).to(device)

    criterion = nn.CrossEntropyLoss(label_smoothing=0.1)

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = ReduceLROnPlateau(optimizer)

    # best_score = 0.
    for epoch in range(num_epochs):
        random.shuffle(train_data)
    
        start = time.time()
        model.train()
        data = []
        for batch in train_data:
            src, tgt = zip(*batch)
            src = torch.stack([src_vocab.numberize(*words) for words in src]).to(device)
            tgt = torch.stack([tgt_vocab.numberize(*words) for words in tgt]).to(device)
            data.append(Batch(src, tgt, pad_idx))
        train_loss = train_epoch(
            data,
            model,
            LossCompute(model.generator, criterion),
            optimizer,
            mode='train',
        )

        model.eval()
        data = []
        for batch in valid_data:
            src, tgt = zip(*batch)
            src = torch.stack([src_vocab.numberize(*words) for words in src]).to(device)
            tgt = torch.stack([tgt_vocab.numberize(*words) for words in tgt]).to(device)
            data.append(Batch(src, tgt, pad_idx))
        valid_loss = train_epoch(
            data,
            model,
            LossCompute(model.generator, criterion),
            mode='eval',
        )
        elapsed = time.time() - start

        scheduler.step(valid_loss)
        lr = optimizer.param_groups[0]['lr']
        print(f'[{epoch + 1}] Train Loss: {train_loss} | Valid Loss: {valid_loss} | Learning Rate: {lr} | Elapsed Time: {elapsed}', flush=True)

        # TODO score_test()
        # candidate, reference = [], []
        # for batch in tqdm.tqdm(valid_data):
        #     for src, tgt in batch:
        #         src = src_vocab.numberize(*src).unsqueeze(0).to(device)
        #         tgt = tgt_vocab.numberize(*tgt).unsqueeze(0).to(device)
        #         batch = Batch(src, tgt, pad_idx)
        #         model_out = greedy_decode(model, batch.src, batch.src_mask)[0]
        #         reference.append(detokenize([tgt_vocab.denumberize(x) for x in batch.tgt[0] if x != pad_idx]))
        #         candidate.append(detokenize([tgt_vocab.denumberize(x) for x in model_out if x != pad_idx]).split('<EOS>')[0] + ' <EOS>')

        # bleu_score = bleu.corpus_score(candidate, [reference])
        # chrf_score = chrf.corpus_score(candidate, [reference])
        # print(chrf_score, ';', bleu_score, flush=True)
        # if bleu_score.score > best_score:
        #     print('saving best model...')
        #     torch.save(model, 'model')
        #     best_score = bleu_score.score
        # print()

def score_test(max_len, data_limit, batch_size, pad_idx=2):
    test_data = []
    for line in open('data/test.bpe.de-en'):
        src_line, tgt_line = line.split('\t')
        src_words = ['<BOS>'] + src_line.split() + ['<EOS>']
        tgt_words = ['<BOS>'] + tgt_line.split() + ['<EOS>']
        if max_len is None or len(src_words) <= max_len:
            test_data.append((src_words, tgt_words))
    test_data = batch_data(test_data[:data_limit], batch_size)

    # TODO store vocab in model
    src_vocab, tgt_vocab = Vocab(), Vocab()
    with open('data/vocab.de') as vocab_file:
        for line in vocab_file.readlines():
            src_vocab.add(line.split()[0])
    with open('data/vocab.en') as vocab_file:
        for line in vocab_file.readlines():
            tgt_vocab.add(line.split()[0])

    model = torch.load('model').to(device)

    candidate, reference = [], []
    with torch.no_grad():
        for batch in tqdm.tqdm(test_data):
            src, tgt = zip(*batch)
            src = torch.stack([src_vocab.numberize(*words) for words in src]).to(device)
            tgt = torch.stack([tgt_vocab.numberize(*words) for words in tgt]).to(device)
            batch = Batch(src, tgt, pad_idx)
            model_out = greedy_decode(model, batch.src, batch.src_mask)
            for i in range(batch_size):
                reference.append(detokenize([tgt_vocab.denumberize(x) for x in batch.tgt[i] if x != pad_idx]))
                candidate.append(detokenize([tgt_vocab.denumberize(x) for x in model_out[i] if x != pad_idx]).split('<EOS>')[0] + ' <EOS>')

    bleu_score = bleu.corpus_score(candidate, [reference])
    chrf_score = chrf.corpus_score(candidate, [reference])
    print(chrf_score, ';', bleu_score, flush=True)
    with open('data/test.out', 'w') as outfile:
        for words in candidate:
            outfile.write(words.split('<BOS> ')[1].split('<EOS>')[0] + '\n')

def translate(text, pad_idx=2):
    text = mt.tokenize(text, return_str=True)
    text = bpe.process_line(text)

    src_vocab, tgt_vocab = Vocab(), Vocab()
    with open('data/vocab.de') as vocab_file:
        for line in vocab_file.readlines():
            src_vocab.add(line.split()[0])
    with open('data/vocab.en') as vocab_file:
        for line in vocab_file.readlines():
            tgt_vocab.add(line.split()[0])

    model = torch.load('model').to(device)

    src = src_vocab.numberize(*text.split()).unsqueeze(0).to(device)
    src_mask = Batch(src, pad=pad_idx).src_mask
    model_out = greedy_decode(model, src, src_mask)[0]
    translation = detokenize([tgt_vocab.denumberize(x) for x in model_out if x != pad_idx])
    return translation.split('<BOS> ')[1].split('<EOS>')[0]

if __name__ == '__main__':
    # train_model(max_len=30, data_limit=1000000, batch_size=16, num_epochs=10, lr=1e-4)
    # score_test(max_len=30, data_limit=1000000, batch_size=16)
    # print(translate('Im Juli, möchte ich nach Europa reisen.'))
    print(translate('Ich sollte meine Hausaufgaben machen, bevor wir heute Abend trinken gehen.'))
    # print(translate('Arjun solltet seine Hausaufgaben machen, bevor wir heute Abend trinken gehen.'))