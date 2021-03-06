import time
from collections import defaultdict
import os
import pickle
from shutil import copy2, SameFileError
import pytablewriter
import pandas as pd

import tensorflow as tf

from src.input_fn import train_eval_input_fn, predict_input_fn
from src.metrics import ner_evaluate
from src.model_fn import BertMultiTask
from src.params import Params
from src.utils import create_path
from src.estimator import Estimator
from src.ckpt_restore_hook import RestoreCheckpointHook


EXPERIMENTS_LIST = [
    {'problems': ['msraner', 'pkucws', 'WeiboNER',
                  'cityucws', 'msrcws',  'bosonner',
                  'CTBCWS', 'CTBPOS'],
     'additional_params': {'label_smoothing': 0.0, 'train_epoch': 30},
     'name': 'baseline_no_label_smooth'},
    {'problems': ['msraner', 'pkucws', 'WeiboNER',
                  'cityucws', 'msrcws',  'bosonner',
                  'CTBCWS', 'CTBPOS'],
     'additional_params': {'init_lr': 5e-6, 'train_epoch': 30},
     'name': 'baseline_no_lr_scale_up'},
    {'problems': ['ontonotes_cws&ontonotes_chunk&ontonotes_ner'],

     'additional_params': {},
     'name': 'ontonotes_multitask'},
    {'problems': ['ontonotes_cws', 'ontonotes_chunk', 'ontonotes_ner'],

     'additional_params': {'train_epoch': 30},
     'name': 'baseline'},
    {'problems': ['pkucws', 'WeiboNER',
                  'cityucws', 'msrcws',  'bosonner',
                  'CTBCWS',  'ascws', 'msraner', 'CTBPOS'],
     'additional_params': {'train_epoch': 30},
     'name': 'baseline'},

    {
        'name': 'multitask_domain_predict',
        'problems': ['WeiboNER&Weibo_domain|bosonner&boson_domain|msraner&msra_domain|ascws&as_domain|pkucws&pku_domain|cityucws&cityu_domain|msrcws&msr_domain'],
        'additional_params': {'train_epoch': 30}
    },
    {
        'name': 'multitask_multiner',
        'problems': ['CWS|POS|WeiboNER|bosonner|msraner'],
        'additional_params': {'train_epoch': 30}
    },
]


def train_problem(params, problem, gpu=4, base='baseline'):
    tf.keras.backend.clear_session()

    if not os.path.exists('tmp'):
        os.mkdir('tmp')

    base = os.path.join('tmp', base)
    params.assign_problem(problem, gpu=int(gpu), base_dir=base)

    create_path(params.ckpt_dir)

    tf.logging.info('Checkpoint dir: %s' % params.ckpt_dir)
    time.sleep(3)

    model = BertMultiTask(params=params)
    model_fn = model.get_model_fn(warm_start=False)

    dist_trategy = tf.contrib.distribute.MirroredStrategy(
        num_gpus=int(gpu),
        cross_tower_ops=tf.contrib.distribute.AllReduceCrossTowerOps(
            'nccl', num_packs=int(gpu)))

    run_config = tf.estimator.RunConfig(
        train_distribute=dist_trategy,
        eval_distribute=dist_trategy,
        log_step_count_steps=params.log_every_n_steps)

    # ws = make_warm_start_setting(params)

    estimator = Estimator(
        model_fn,
        model_dir=params.ckpt_dir,
        params=params,
        config=run_config)
    train_hook = RestoreCheckpointHook(params)

    def train_input_fn(): return train_eval_input_fn(params)
    estimator.train(
        train_input_fn, max_steps=params.train_steps, hooks=[train_hook])

    return estimator


def eval_single_problem(params, problem, label_encoder_path, estimator, gpu=4, base='baseline'):

    params.assign_problem(problem, gpu=int(gpu), base_dir=base)
    eval_dict = {}

    # copy label encoder
    try:
        copy2(label_encoder_path, os.path.join(
            params.ckpt_dir, '%s_label_encoder.pkl' % problem))
    except SameFileError:
        pass

    def input_fn(): return train_eval_input_fn(params, mode='eval')
    if 'ner' not in problem and 'NER' not in problem:
        eval_dict.update(estimator.evaluate(input_fn=input_fn))
    else:

        raw_ner_eval = ner_evaluate(problem, estimator, params)
        rename_dict = {}
        rename_dict['%s_Accuracy' % problem] = raw_ner_eval['Acc']
        rename_dict['%s_F1 Score' % problem] = raw_ner_eval['F1']
        rename_dict['%s_Precision' % problem] = raw_ner_eval['Precision']
        rename_dict['%s_Recall' % problem] = raw_ner_eval['Recall']
        eval_dict.update(rename_dict)

    return eval_dict


def eval_problem(params, raw_problem, estiamtor, gpu=4, base='baseline'):

    # set bigger max seq len
    params.max_seq_len = 350

    eval_problem_list = []
    base = os.path.join('tmp', base)
    eval_label_encoder_list = []
    for sub_problem in raw_problem.split('|'):
        for single_problem in sub_problem.split('&'):
            eval_problem_list.append([single_problem])
            if single_problem == 'CWS':
                eval_problem_list[-1] += ['ascws', 'msrcws', 'pkucws',
                                          'cityucws', 'CTBCWS']

            elif single_problem == 'NER':
                eval_problem_list[-1] += ['WeiboNER', 'bosonner', 'msraner']
            elif single_problem == 'POS':
                eval_problem_list[-1] += ['CTBPOS']

            eval_label_encoder_list.append(os.path.join(
                params.ckpt_dir, '%s_label_encoder.pkl' % single_problem))

    final_eval_dict = {}
    for problem_list, label_encoder_path in zip(
            eval_problem_list, eval_label_encoder_list):
        for problem in problem_list:
            final_eval_dict.update(eval_single_problem(
                params,
                problem=problem,
                label_encoder_path=label_encoder_path,
                estimator=estiamtor,
                gpu=gpu,
                base=base))

    params.max_seq_len = 128

    return final_eval_dict


def create_result_table(group_by='problem'):
    with open('tmp/results.pkl', 'rb') as f:
        result_dict = pickle.load(f)

    table_list = []

    if group_by == 'problem':
        problem_list = list(
            set([
                k
                for problem_set in result_dict.values()
                for k in problem_set.keys()]))
        problem_list = set(['_'.join(p.split('_')[:-1]) for p in problem_list if p.split('_')[
                           0] not in ['loss', 'global']])
        for problem in problem_list:
            writer = pytablewriter.MarkdownTableWriter()
            writer.table_name = problem
            problem_result_dict = {
                '%s_Accuracy' % problem: [],
                '%s_F1 Score' % problem: [],
                '%s_Precision' % problem: [],
                '%s_Recall' % problem: [],
                '%s_Accuracy Per Sequence' % problem: [],
                '%s_Approximate BLEU' % problem: []
            }
            name = []
            for experiment_name, experiment_result in result_dict.items():
                name.append(experiment_name)
                for metric in problem_result_dict:
                    if metric in experiment_result:
                        problem_result_dict[metric].append(
                            experiment_result[metric])
                    else:
                        problem_result_dict[metric].append('-')

            problem_result_dict['experiment'] = name

            # only keep columns with results
            for key in list(problem_result_dict.keys()):
                if len(set(problem_result_dict[key])) == 1:
                    del problem_result_dict[key]

            # put name in the first col
            df = pd.DataFrame(problem_result_dict)

            # only keep set with results
            keep_row = []
            for row_ind, row in df.iterrows():
                if len(set(row)) > 2:
                    keep_row.append(row_ind)
            df = df.iloc[keep_row]

            cols = df.columns.tolist()
            cols = cols[-1:] + cols[:-1]
            df = df[cols]
            writer.from_dataframe(df)

            table_list.append(writer.dumps())

    write_str = ''.join(table_list)
    with open('baseline.md', 'w', encoding='utf8') as f:
        f.writelines(write_str)


def main():
    gpu = 4
    params = Params()

    if os.path.exists('tmp/results.pkl'):
        with open('tmp/results.pkl', 'rb') as f:
            result_dict = pickle.load(f)
    else:
        result_dict = defaultdict(dict)
    for experiment_set in EXPERIMENTS_LIST:
        print('Running Problem set %s' % experiment_set['name'])
        params = Params()

        if experiment_set['additional_params']:
            for k, v in experiment_set['additional_params'].items():
                setattr(params, k, v)

        for problem in experiment_set['problems']:
            if '%s_Accuracy' % problem not in result_dict[experiment_set['name']]:
                estiamtor = train_problem(
                    params, problem, gpu, experiment_set['name'])
                eval_dict = eval_problem(
                    params, problem, estiamtor, gpu, base=experiment_set['name'])
                result_dict[experiment_set['name']].update(eval_dict)
                print(result_dict)
                pickle.dump(result_dict, open('tmp/results.pkl', 'wb'))

    print(result_dict)

    pickle.dump(result_dict, open('tmp/results.pkl', 'wb'))
    create_result_table()


if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.DEBUG)
    main()
