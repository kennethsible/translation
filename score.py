from comet import download_model, load_from_checkpoint
from sacrebleu.metrics import BLEU, CHRF
from manager import Manager
from decode import beam_search
from datetime import timedelta
import torch, time, tqdm

def score_model(manager, logger, use_tqdm=False):
    model, vocab = manager.model, manager.vocab
    candidate, reference = [], []

    start = time.perf_counter()
    model.eval()
    with torch.no_grad():
        for batch in tqdm.tqdm(manager.test, disable=(not use_tqdm)):
            src_encs = model.encode(batch.src_nums, batch.src_mask)
            for i in tqdm.tqdm(range(src_encs.size(0)), leave=False, disable=(not use_tqdm)):
                out_nums = beam_search(manager, src_encs[i], batch.src_mask[i], manager.beam_size)
                reference.append(manager.detokenize(vocab.denumberize(*batch.tgt_nums[i])))
                candidate.append(manager.detokenize(vocab.denumberize(*out_nums)))
    elapsed = timedelta(seconds=(time.perf_counter() - start))

    bleu_score = BLEU().corpus_score(candidate, [reference])
    chrf_score = CHRF().corpus_score(candidate, [reference])

    samples = []
    for i, batch in enumerate(manager.test):
        for j, src_nums in enumerate(batch.src_nums):
            src_words = manager.detokenize(vocab.denumberize(*src_nums), manager.src_lang)
            samples.append({'src': src_words, 'mt': candidate[i + j], 'ref': reference[i + j]})
    comet_model = load_from_checkpoint(download_model('Unbabel/wmt22-comet-da'))
    comet_score = comet_model.predict(samples)['system_score']

    checkpoint = f'BLEU = {bleu_score.score:.16f}'
    checkpoint += f' | CHRF = {chrf_score.score:.16f}'
    checkpoint += f' | COMET = {comet_score:.16f}'
    checkpoint += f' | Elapsed Time = {elapsed}'
    logger.info(checkpoint)

    return bleu_score, chrf_score, comet_score, candidate

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', metavar='FILE', required=True, help='testing data')
    parser.add_argument('--load', metavar='FILE', required=True, help='load model')
    parser.add_argument('--tqdm', action='store_true', help='enable tqdm')
    args, unknown = parser.parse_known_args()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model_dict = torch.load(args.load, map_location=device)
    src_lang, tgt_lang = model_dict['src_lang'], model_dict['tgt_lang']
    vocab_file, codes_file = model_dict['vocab_file'], model_dict['codes_file']

    config = model_dict['config']
    for i, arg in enumerate(unknown):
        if arg[:2] == '--' and len(unknown) > i:
            option, value = arg[2:].replace('-', '_'), unknown[i + 1]
            config[option] = (int if value.isdigit() else float)(value)

    with open(args.data) as test_file:
        manager = Manager(src_lang, tgt_lang, vocab_file, codes_file,
            args.load, config, device, data_file=None, test_file=test_file)
    manager.model.load_state_dict(model_dict['state_dict'])

    if torch.cuda.get_device_capability()[0] >= 8:
        torch.set_float32_matmul_precision('high')
    # if torch.__version__ >= '2.0':
    #     manager.model = torch.compile(manager.model)

    logger = logging.getLogger('torch.logger')

    *_, candidate = score_model(manager, logger, args.tqdm)
    print('', *candidate, sep='\n')

if __name__ == '__main__':
    import argparse, logging, sys
    main()
