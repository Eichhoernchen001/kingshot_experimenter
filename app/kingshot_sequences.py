"""Numbered experiment designs and explicit response perspective."""
import copy
import csv
import hashlib
import json
import re
import os
from pathlib import Path

SECTIONS=('lead_troop','joiner')
def opposite(side):return 'defender' if side=='attacker' else 'attacker'
def has_pool(design,side):return any(design.get(side,{}).get('pools',{}).values())
def varied_side(design,section):
    if section=='lead_troop':return opposite(design.get('fixed_side','attacker'))
    sides=[s for s in ('attacker','defender') if has_pool(design,s)]
    if len(sides)>1:raise ValueError('Only one side may have an active joiner pool. Select which side varies on the Joiners page.')
    return sides[0] if sides else design.get('varied_side','attacker')

def migrate_lead(design):
    """Split older Cartesian designs so every experiment has one fixed setup."""
    if design.get('fixed_side') in ('attacker','defender'):return [copy.deepcopy(design)]
    counts={s:sum(len(f.get('troop_variants_pct',[])) for f in design[s]['formations']) for s in ('attacker','defender')}
    fixed=min(counts,key=counts.get)
    result=[]
    for formation in design[fixed]['formations']:
        for split in formation['troop_variants_pct']:
            item=copy.deepcopy(design);item['fixed_side']=fixed
            f=copy.deepcopy(formation);f.update(troop_variants_pct=[split],troop_variant_groups=[1],troop_range_lines=['/'.join(f'{x:g}' for x in split)])
            item[fixed]['formations']=[f];result.append(item)
    return result or [copy.deepcopy(design)]

def ensure_sequences(cfg):
    from kingshot_profile_template import initialize_formation
    from kingshot_players import migrate_players
    migrate_players(cfg)
    sequences=cfg.setdefault('experiments',{})
    for section in SECTIONS:
        if not sequences.get(section):
            sequences[section]=migrate_lead(cfg[section]) if section=='lead_troop' else [copy.deepcopy(cfg[section])]
        for item in sequences[section]:
            if section=='joiner':item.setdefault('varied_side','defender' if has_pool(item,'defender') and not has_pool(item,'attacker') else 'attacker')
            from kingshot_players import assignment
            for side,letter in assignment(item).items():
                formations=item[side]['formations'] if section=='lead_troop' else [item[side]]
                for f in formations:initialize_formation(cfg['profiles'][letter],f)
        index=min(max(int(cfg.get('experiment_selection',{}).get(section,0)),0),len(sequences[section])-1)
        cfg.setdefault('experiment_selection',{})[section]=index
        cfg[section]=copy.deepcopy(sequences[section][index])
    return cfg

def design_errors(design,section):
    errors=[]
    from kingshot_players import assignment
    if assignment(design) not in ({'attacker':'A','defender':'B'},{'attacker':'B','defender':'A'}):errors.append('Assign different players to attacker and defender.')
    try:side=varied_side(design,section)
    except ValueError as exc:return [str(exc)]
    if side not in ('attacker','defender'):errors.append('Choose an attacker or defender varying side.')
    if section=='lead_troop':
        fixed=opposite(side);items=design.get(fixed,{}).get('formations',[])
        if len(items)!=1 or len(items[0].get('troop_variants_pct',[]))!=1:
            errors.append('The first (fixed) section must contain exactly one formation and one troop split.')
    return errors

def duplicate_groups(cfg,sections):
    result=[]
    for section in sections:
        seen={}
        for i,design in enumerate(cfg['experiments'][section],1):
            clean=copy.deepcopy(design)
            # Ignore labels and unused cached hero settings; compare actual inputs.
            for side in ('attacker','defender'):
                formations=clean[side]['formations'] if section=='lead_troop' else [clean[side]]
                for f in formations:
                    f.pop('troop_range_lines',None)
                    names={x if isinstance(x,str) else x['name'] for x in f.get('leads',{}).values()}
                    f['hero_settings']={k:v for k,v in f.get('hero_settings',{}).items() if k in names}
                    for values in f.get('pools',{}).values():values.sort()
            key=json.dumps(clean,sort_keys=True,separators=(',',':'))
            seen.setdefault(key,[]).append(i)
        result += [(section,ids) for ids in seen.values() if len(ids)>1]
    return result

def child_folder_name(cfg,section,design,index):
    from kingshot_run_history import player_names
    from kingshot_players import player_names
    names=player_names(cfg,design=design)
    def safe(value):return re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+','_',str(value)).strip(' ._') or 'Player'
    if section=='lead_troop':return f'{index:03d}_'+safe(names['attacker'])+'_vs_'+safe(names['defender'])
    parts=[]
    for side in ('attacker','defender'):
        heroes='-'.join(str(design[side]['leads'][r]) for r in ('inf','cav','arch'))
        split='-'.join(f'{float(x):g}' for x in design[side]['troop_percentages'])
        parts.append(safe(names[side])+'_'+safe(heroes)+'_'+split)
    label=f'{index:03d}_'+parts[0]+'_vs_'+parts[1]
    return label if len(label)<=150 else label[:137]+'_'+hashlib.sha256(label.encode()).hexdigest()[:12]

def recorded_winrate(attacker_winrate,side):
    if side not in ('attacker','defender'):raise ValueError('Unknown win-rate side.')
    value=float(attacker_winrate)
    return value if side=='attacker' else 100.0-value

def prepare_resume_perspective(path,side):
    """Old CSVs were attacker-only; explicitly label/convert once when resumed."""
    path=Path(path)
    if not path.is_file() or not path.stat().st_size:return
    with path.open(newline='',encoding='utf-8-sig') as handle:
        reader=csv.DictReader(handle);fields=list(reader.fieldnames or []);rows=list(reader)
    if 'winrate_side' in fields:
        found={r.get('winrate_side') for r in rows if r.get('winrate')}
        if found and found!={side}:raise ValueError('Existing CSV records a different win-rate side. Choose a new results folder.')
        return
    fields.append('winrate_side')
    for row in rows:
        if row.get('winrate'):row['winrate']=recorded_winrate(float(row['winrate']),side)
        row['winrate_side']=side
    with path.open('w',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def filesystem_folder(path):
    """Keep descriptive names usable beyond Windows' legacy 260-character limit."""
    path=Path(path).resolve()
    text=str(path)
    if os.name=='nt' and not text.startswith('\\\\?\\'):
        text=('\\\\?\\UNC\\'+text[2:]) if text.startswith('\\\\') else ('\\\\?\\'+text)
        return Path(text)
    return path


def display_path(path):
    text=str(path)
    if text.startswith('\\\\?\\UNC\\'):return Path('\\\\'+text[8:])
    return Path(text[4:]) if text.startswith('\\\\?\\') else Path(text)
