"""Result locations and data-only import/export of experiment configurations."""
from pathlib import Path
import ast
import copy
import json
import re

FOLDERS = {'lead_troop': 'lead_troop_experiment', 'joiner': 'joiner_experiment', 'all': 'all_experiments'}
SNAPSHOT = 'run_configuration.json'
DEFAULT_FOLDER_LABEL = '(Default folder)'


def player_names(cfg,section=None):
    from kingshot_players import player_names as names
    return names(cfg,section)


def automatic_result_name(cfg, section):
    names = player_names(cfg, section if section != "all" else "lead_troop")
    def safe(value):
        return re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', '_', value).strip(' ._') or 'Player'
    a, b = safe(names['attacker']), safe(names['defender'])
    if section in ('lead_troop','all') or len(cfg.get('experiments',{}).get(section,[])) > 1:
        return f'{a}_vs_{b}'
    def ratio(side):
        return '-'.join(f'{float(x):g}' for x in cfg['joiner'][side]['troop_percentages'])
    return f'{a}_{ratio("attacker")}_vs_{b}_{ratio("defender")}'


def configured_result_folder(results, cfg, section):
    chosen = cfg.get('result_names', {}).get(section, '').strip()
    if chosen == DEFAULT_FOLDER_LABEL:
        return result_folder(results, section)
    return result_folder(results, section, chosen or automatic_result_name(cfg, section))


def result_folder(results, section, name=''):
    name = name.strip()
    if name and (name in ('.', '..') or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                 or name.endswith(('.', ' '))
                 or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                     *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
        raise ValueError('Use a folder name without path separators or reserved characters.')
    base = Path(results) / FOLDERS[section]
    folder = base / name if name else base
    if not folder.resolve().is_relative_to(base.resolve()):
        raise ValueError('Results folder must stay inside its experiment folder.')
    return folder


def write_snapshot(folder, cfg, section, root):
    folder, root = Path(folder), Path(root)
    saved = copy.deepcopy(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    # Configuration contains all imported values; archives are never run inputs.
    for name in ('kingshot_hero_data.json', 'player_template.json'):
        (folder / ('reference_' + name)).write_bytes((root / 'json' / name).read_bytes())
    payload = {'experiment_type': section, 'configuration': saved}
    (folder / SNAPSHOT).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def previous_runs(results, section):
    from kingshot_sequences import filesystem_folder, display_path
    results=filesystem_folder(results);base=results/FOLDERS[section]
    marker='kingshot_winrates' if section=='joiner' else 'kingshot_lead_troop_winrates'
    candidates=set()
    if base.is_dir():candidates.update([base,*(p for p in base.iterdir() if p.is_dir()),*(p.parent for p in base.rglob(SNAPSHOT))])
    all_base=results/FOLDERS['all']
    if all_base.is_dir():
        for snapshot in all_base.rglob(SNAPSHOT):
            try:kind=json.loads(snapshot.read_text(encoding='utf-8-sig')).get('experiment_type')
            except (OSError,ValueError):continue
            if kind in (section,'all'):candidates.add(snapshot.parent)
    return [display_path(p) for p in sorted(candidates,key=lambda p:str(p).casefold())
            if (p/SNAPSHOT).is_file() or (p/'kingshot_config.json').is_file()
            or (p/f'{marker}_experiment_settings.json').is_file() or (p/'experiment_settings.txt').is_file()]


def _literal(text, label):
    match = re.search(r'^' + re.escape(label) + r'\s*=\s*', text, re.M)
    if not match:
        raise KeyError(label)
    tail = text[match.end():]
    try:
        return json.JSONDecoder().raw_decode(tail)[0]
    except ValueError:
        # Older writers use Python literals. Never execute settings text.
        return ast.literal_eval(tail.splitlines()[0])


def import_run(folder, section, current, root):
    from kingshot_config import _migrate_to_current
    from kingshot_sequences import filesystem_folder
    folder = filesystem_folder(folder)
    snapshot = folder / SNAPSHOT
    if snapshot.is_file() or (folder / 'kingshot_config.json').is_file():
        raw = json.loads((snapshot if snapshot.is_file() else folder / 'kingshot_config.json').read_text(encoding='utf-8-sig'))
        if snapshot.is_file():
            if raw.get('experiment_type') not in (section,'all'):
                raise ValueError('This folder contains a different experiment type.')
            raw = raw['configuration']
        if not isinstance(raw, dict) or not all(k in raw for k in ('profiles', section, 'run', 'battle_setup')):
            raise ValueError('The saved configuration is incomplete.')
        raw = copy.deepcopy(raw)
        for p in raw['profiles'].values():
            path = Path(p['player_file'])
            if int(raw.get('schema_version', 1)) < 10 and not path.is_absolute():
                p['player_file'] = str((folder / path).resolve())
        for k, value in raw.get('lookups', {}).items():
            path = Path(value)
            if not path.is_absolute():
                raw['lookups'][k] = str((folder / path).resolve())
        saved = _migrate_to_current(Path(root), raw)
        notes = list(saved.get("migration_notes", []))
    else:
        saved, notes = _import_legacy(folder, section, current, root)
    merged = copy.deepcopy(current)
    # The other experiment design stays intact; shared inputs necessarily change.
    for key in ('profiles', 'player_setup', 'battle_setup', 'run', 'lookups', 'assignment', 'battle_options', section,
                'analysis' if section == 'joiner' else 'plotter'):
        if key in saved:
            merged[key] = copy.deepcopy(saved[key])
    merged.setdefault('experiments',{})[section]=copy.deepcopy(saved.get('experiments',{}).get(section,[saved[section]]))
    merged.setdefault('experiment_selection',{})[section]=saved.get('experiment_selection',{}).get(section,0)
    from kingshot_profile_template import detach_config
    detach_config(merged, root, read_legacy=False)
    from kingshot_sequences import ensure_sequences
    ensure_sequences(merged)
    merged["migration_notes"] = list(dict.fromkeys(notes + merged.get("migration_notes", [])))
    notes = merged["migration_notes"]
    return merged, notes


def _import_legacy(folder, section, current, root):
    cfg = copy.deepcopy(current)
    stem = 'kingshot_winrates' if section == 'joiner' else 'kingshot_lead_troop_winrates'
    path = folder / f'{stem}_experiment_settings.json'
    data = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    if isinstance(data.get('configuration'), dict):
        from kingshot_config import _migrate_to_current
        return _migrate_to_current(Path(root), data['configuration']), []
    text_path = folder / 'experiment_settings.txt'
    text = text_path.read_text(encoding='utf-8-sig') if text_path.is_file() else ''
    exp = data.get(FOLDERS[section], {})
    prog = data.get('profile_progression', {})
    options = data.get('profile_options', {})
    restored = []
    def assign(owner, key, label):
        try:
            owner[key] = _literal(text, label)
            restored.append(label)
            return True
        except KeyError:
            return False
    for key, label in [('simulations_per_batch', 'SIMULATIONS_PER_BATCH'), ('batches_per_condition', 'BATCHES_PER_CONDITION'),
                       ('resume', 'RESUME_FROM_CSV'), ('headless', 'HEADLESS'), ('timeout_seconds', 'TIMEOUT_SECONDS'),
                       ('delay_between_batches_seconds', 'DELAY_BETWEEN_BATCHES_SECONDS')]:
        source = 'batches_per_condition_target' if key == 'batches_per_condition' else key
        if source in exp:
            cfg['run'][key] = exp[source]; restored.append(source)
        assign(cfg['run'], key, label)
    for side, letter, prefix, short in [('attacker', 'A', 'ATTACK', 'atk'), ('defender', 'B', 'DEFENSE', 'def')]:
        p = cfg['profiles'][letter]
        if side in data.get('player_names', {}):
            p['name'] = data['player_names'][side]
        for key in ('total_troops', 'troop_quality'):
            if f'{side}_{key}' in exp:
                cfg['battle_setup'][side][key] = exp[f'{side}_{key}']; restored.append(f'{side}_{key}')
            assign(cfg['battle_setup'][side], key, f'{prefix}_{key.upper()}')
        if exp.get(f'{side}_base_stat_override') is not None:
            p['base_stats'] = exp[f'{side}_base_stat_override']; restored.append(f'{side} base stats')
        side_prog = prog.get(side, {})
        if 'configured_progression' in side_prog:
            p['hero_progression'] = side_prog['configured_progression']
            p['gear_enabled'] = p['hero_progression'].get('gear_enabled', p['gear_enabled'])
            restored.append(f'{side} progression')
        if 'hero_gear' in side_prog:
            p['hero_gear'] = side_prog['hero_gear']
        if f'special_bonuses_{side}' in options:
            p['special_bonuses'] = options[f'special_bonuses_{side}']
        if side in prog.get('apply_special_bonuses', {}):
            p['special_bonuses_enabled'] = prog['apply_special_bonuses'][side]
        if section == 'joiner':
            dest = cfg[section][side]
            if f'{side}_troop_percentages' in exp:
                value = exp[f'{side}_troop_percentages']
                dest['troop_percentages'] = [value[k] for k in ('infantry', 'cavalry', 'archers')] if isinstance(value, dict) else value
            if f'{side}_lead_heroes' in exp:
                dest['leads'] = dict(zip(('inf', 'cav', 'arch'), exp[f'{side}_lead_heroes']))
            if side in prog.get('formation_widget_buffs', {}):
                dest['widget_buffs'] = prog['formation_widget_buffs'][side]
            for n in (1, 2, 3, 4):
                assign(dest['pools'], str(n), f'joiner_pool_{short}_max_{n}')
                holder = {}
                if assign(holder, 'value', f'joiner{n}_{short}'):
                    dest['manual_slots'][n-1] = holder['value']
        else:
            assign(cfg[section][side], 'formations', 'ATTACK_FORMATIONS' if side == 'attacker' else 'DEFENSE_SCENARIOS')
    if section == 'lead_troop':
        assign(cfg[section], 'only_matchups', 'ONLY_MATCHUPS')
    if not restored:
        raise ValueError('No supported settings were found in this folder.')
    # Older files used shared profile progression; reconstruct formation settings.
    from kingshot_profile_template import initialize_formation
    for side, letter in [('attacker', 'A'), ('defender', 'B')]:
        formations = [cfg[section][side]] if section == 'joiner' else cfg[section][side]['formations']
        for formation in formations:
            formation.pop('hero_settings', None); formation.pop('add_hero_stats', None)
            initialize_formation(cfg['profiles'][letter], formation)
    cfg.setdefault('experiments',{}).pop(section,None)
    if section=='lead_troop':cfg[section].pop('fixed_side',None)
    from kingshot_sequences import ensure_sequences
    ensure_sequences(cfg)
    return cfg, ['This older run has no complete configuration snapshot. Restored the settings available in its files; missing values retain your current settings. Baseline player files and lookup files remain unchanged. Review all tabs before running.',
                 'Restored: ' + ', '.join(restored)]
