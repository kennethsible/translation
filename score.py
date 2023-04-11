from comet import download_model, load_from_checkpoint
from sacrebleu.metrics import BLEU, CHRF
from manager import Manager, Tokenizer
from decode import beam_decode
from datetime import timedelta
import torch, time, toml

def score_model(manager, tokenizer, model_file=None, *, indent=0):
    candidate, reference = [], []

    start = time.perf_counter()
    manager.model.eval()
    with torch.no_grad():
        for batch in manager.data:
            src_encs = manager.model.encode(batch.src_nums, batch.src_mask)
            for i in range(src_encs.size(0)):
                out_nums = beam_decode(manager, src_encs[i], batch.src_mask[i], manager.config['beam_width'])
                reference.append(tokenizer.detokenize(manager.vocab.denumberize(*batch.tgt_nums[i]), manager.tgt_lang))
                candidate.append(tokenizer.detokenize(manager.vocab.denumberize(*out_nums), manager.tgt_lang))
    elapsed = timedelta(seconds=(time.perf_counter() - start))

    bleu_score = BLEU().corpus_score(candidate, [reference])
    chrf_score = CHRF().corpus_score(candidate, [reference])

    samples, tokenizer = [], Tokenizer(manager.src_lang)
    for i, batch in enumerate(manager.data):
        for j, src_nums in enumerate(batch.src_nums):
            src_words = tokenizer.detokenize(manager.vocab.denumberize(*src_nums), manager.src_lang)
            samples.append({'src': src_words, 'mt': candidate[i + j], 'ref': reference[i + j]})
    comet_model = load_from_checkpoint(download_model('Unbabel/wmt22-comet-da'))
    comet_score = comet_model.predict(samples)['system_score']

    checkpoint = indent * ' '
    checkpoint += f'BLEU = {bleu_score.score:.4f}'
    checkpoint += f' | CHRF = {chrf_score.score:.4f}'
    checkpoint += f' | COMET = {comet_score:.4f}'
    checkpoint += f' | Elapsed Time = {elapsed}'
    if model_file:
         with open(model_file + '.log', 'a') as file:
             file.write(checkpoint + '\n')
    print(checkpoint)

    return bleu_score, chrf_score, candidate

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lang', nargs=2, required=True, help='source/target language')
    parser.add_argument('--data', metavar='FILE', help='testing data')
    parser.add_argument('--vocab', metavar='FILE', help='shared vocab')
    parser.add_argument('--config', metavar='FILE', help='model config')
    parser.add_argument('--load', metavar='FILE', help='load state_dict')
    args, unknown = parser.parse_known_args()

    src_lang, tgt_lang = args.lang
    if not args.config:
        args.config = 'model.config'
    with open(args.config) as file:
        config = toml.load(file)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    for i, arg in enumerate(unknown):
        if arg[:2] == '--' and len(unknown) > i:
            option, value = arg[2:], unknown[i + 1]
            config[option] = (int if value.isdigit() else float)(value)

    if not args.data:
        args.data = f'data/testing/data.tok.bpe.{src_lang}{tgt_lang}'
    if not args.vocab:
        args.vocab = f'data/vocab.{src_lang}{tgt_lang}'
    if not args.load:
        args.load = f'data/model.{src_lang}{tgt_lang}'

    manager = Manager(
        src_lang,
        tgt_lang,
        config,
        device,
        args.vocab,
        args.data
    )
    if args.load:
        manager.load_model(args.load)
    tokenizer = Tokenizer(src_lang, tgt_lang)

    _, _, candidate = score_model(manager, tokenizer)
    print('', *candidate, sep='\n')

if __name__ == '__main__':
    import argparse
    main()
