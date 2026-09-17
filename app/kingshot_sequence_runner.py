"""Run or plot numbered experiment sequences in one cancellable process."""
import argparse
import asyncio
import copy
import importlib
import json
from pathlib import Path
import sys
from kingshot_paths import ROOT_DIR, RESULTS_DIR
from kingshot_config import load_config, validate_config, apply_config_to_globals
from kingshot_sequences import SECTIONS, ensure_sequences, varied_side, child_folder_name, duplicate_groups, filesystem_folder
from kingshot_run_history import configured_result_folder, write_snapshot

SCRIPT={'joiner':'kingshot_joiner_experiment','lead_troop':'kingshot_lead_troop_experiment'}
CSV={'joiner':'kingshot_winrates.csv','lead_troop':'kingshot_lead_troop_winrates.csv'}

def build_jobs(cfg,section,parent=None):
    cfg=ensure_sequences(copy.deepcopy(cfg));sections=SECTIONS if section=='all' else (section,)
    parent=Path(parent) if parent is not None else configured_result_folder(RESULTS_DIR,cfg,section)
    jobs=[]
    for kind in sections:
        for index,design in enumerate(cfg['experiments'][kind],1):
            single=copy.deepcopy(cfg);single[kind]=copy.deepcopy(design)
            single['experiments'][kind]=[copy.deepcopy(design)];single['experiment_selection'][kind]=0
            single['winrate_side']=varied_side(design,kind)
            name=child_folder_name(cfg,kind,design,index)
            if section=='all':name=f'{len(jobs)+1:03d}_{kind}_'+name.split('_',1)[1]
            jobs.append((kind,index,single,parent/name))
    return parent,jobs

async def execute_jobs(cfg,section,preview=False,parent=None):
    parent,jobs=build_jobs(cfg,section,parent)
    parent=filesystem_folder(parent)
    jobs=[(kind,index,single,filesystem_folder(folder)) for kind,index,single,folder in jobs]
    errors,_=validate_config(ROOT_DIR,cfg)
    if errors:raise ValueError('\n'.join(errors))
    for kind,ids in duplicate_groups(cfg,SECTIONS if section=='all' else (section,)):
        print(f'NOTICE: identical {kind} experiments: '+', '.join(map(str,ids)),flush=True)
    if not preview:
        parent.mkdir(parents=True,exist_ok=True)
        write_snapshot(parent,cfg,section,ROOT_DIR)
        (parent/'sequence_manifest.json').write_text(json.dumps({'experiments':[{'type':kind,'number':index,'folder':folder.name,'winrate_side':single['winrate_side']} for kind,index,single,folder in jobs]},indent=2),encoding='utf8')
    for position,(kind,index,single,folder) in enumerate(jobs,1):
        print(f'\n=== Experiment {position}/{len(jobs)}: {kind} #{index}; {single["winrate_side"]} win chance ===\nOutput: {folder}',flush=True)
        module=importlib.import_module(SCRIPT[kind])
        apply_config_to_globals(vars(module),kind,config=single,output_folder=folder)
        if preview:module.preview_experiment()
        else:await module.run_experiment()
    print('Sequence preview complete.' if preview else 'All queued experiments finished.',flush=True)

def plot_saved(cfg,section,parent):
    parent=filesystem_folder(parent)
    sections=SECTIONS if section=='all' else (section,)
    manifest=parent/'sequence_manifest.json'
    entries=json.loads(manifest.read_text(encoding='utf-8-sig')).get('experiments',[]) if manifest.is_file() else None
    def result_paths(kind):
        if entries is None:return sorted(parent.rglob(CSV[kind]))
        paths=[]
        for item in entries:
            if item.get('type')!=kind:continue
            folder=(parent/item['folder']).resolve()
            if not folder.is_relative_to(parent):raise ValueError('Sequence folder must stay inside its results folder.')
            path=folder/CSV[kind]
            if path.is_file():paths.append(path)
        return paths
    found=False
    # Import the scientific model before plotting libraries when both are used.
    if 'joiner' in sections and result_paths('joiner'):
        importlib.import_module('kingshot_full_model_analysis')
    for kind in sections:
        paths=result_paths(kind)
        for path in paths:
            found=True
            print(f'\nCreating plots for {path.parent.name}',flush=True)
            if kind=='lead_troop':
                module=importlib.import_module('kingshot_lead_troop_plotter')
                args=['--experiment-folder',str(path.parent)]
            else:
                module=importlib.import_module('kingshot_full_model_analysis')
                a=cfg['analysis']
                settings=path.with_name(path.stem+'_experiment_settings.json')
                metadata=json.loads(settings.read_text(encoding='utf-8-sig')) if settings.is_file() else {}
                trials=metadata.get('configuration',{}).get('run',{}).get('simulations_per_batch',metadata.get('joiner_experiment',{}).get('simulations_per_batch',100))
                args=[str(path),'--side','auto','--trials-per-row',str(trials),'--existing-output','overwrite','--top-n',str(a['top_n'])]
                for layer in ('pair','triple'):
                    args += [f'--{layer}-selection-method',a[f'{layer}_selection_method'],f'--{layer}-synergy-threshold',str(a[f'{layer}_synergy_threshold']),f'--{layer}-synergy' if a.get(f'{layer}_synergy',True) else f'--no-{layer}-synergy']
            original=sys.argv
            try:sys.argv=[module.__file__,*args];module.main()
            finally:sys.argv=original
    if not found:raise ValueError(f'No matching result CSVs found in {parent}')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('section',choices=(*SECTIONS,'all'))
    parser.add_argument('--mode',choices=('run','preview','plot'),default='run')
    parser.add_argument('--folder',type=Path)
    args=parser.parse_args();cfg=load_config(ROOT_DIR)
    if args.mode=='plot':plot_saved(cfg,args.section,args.folder or configured_result_folder(RESULTS_DIR,cfg,args.section))
    else:asyncio.run(execute_jobs(cfg,args.section,preview=args.mode=='preview',parent=args.folder))

if __name__=='__main__':main()
