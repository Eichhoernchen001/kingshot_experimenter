"""Self-contained player imports and formation-scoped hero settings."""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
from kingshot_progression import (STAT_NAMES, default_gear_set, profile_hero_config,
    layered_hero_stats, widget_skill_rank, validate_gear_piece, GEAR_SLOTS)

ROLES = {'inf': 'inf', 'cav': 'lanc', 'arch': 'mark'}

def stat_vector(value):
    result = {k: float(value.get(k, 0)) for k in STAT_NAMES}
    if any(not math.isfinite(v) or v < 0 for v in result.values()):
        raise ValueError('Stats must be finite, non-negative numbers.')
    return result

def read_player(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('stats'), dict):
        raise ValueError('Player JSON must contain a root stats object.')
    return data

def import_player_values(profile, data, catalog):
    """Import once. Aggregate hero stats are retained exactly; stars are not inferred."""
    result = copy.deepcopy(profile)
    result['base_stats'] = {kind: stat_vector(data['stats'].get(kind, {})) for kind in ROLES.values()}
    if isinstance(data.get('special_bonuses'), dict):
        special = result.setdefault('special_bonuses', {})
        for key in ('petLevels', 'city'):
            if key in data['special_bonuses']:
                special[key] = copy.deepcopy(data['special_bonuses'][key])
    result['imported_specials_included'] = bool(data.get('special_bonuses', {}).get('includedInStats', False))
    result['special_bonuses_enabled'] = not result['imported_specials_included']
    imported = {}
    ranks = {}
    for name, entry in catalog.items():
        aliases = {str(x).casefold() for x in [name, entry.get('simulator_name', name), *entry.get('aliases', [])]}
        found = next((h for n, h in data.get('heroes', {}).items() if n.casefold() in aliases), None)
        if isinstance(found, dict) and isinstance(found.get('stats'), dict):
            imported[name] = stat_vector(found['stats'])
            rank = found.get('widget_level', 0)
            if not 0 <= int(rank) <= 5: raise ValueError(f'{name}: widget skill rank must be 0–5.')
            ranks[name] = int(rank)
    result['imported_hero_stats'] = imported
    result['imported_widget_ranks'] = ranks
    result['imported_stats_include_heroes'] = bool(data.get('stats_include_heroes', False))
    # Explicitly exported editable settings take precedence over aggregate totals.
    if isinstance(data.get('hero_gear'), dict):
        result['hero_gear'] = copy.deepcopy(data['hero_gear'])
    result['prefer_imported_stats'] = True
    result['input_values_embedded'] = True
    return result

def default_hero_setting(profile, name, role):
    prog = profile.get('hero_progression', {})
    h = profile_hero_config(profile, name)
    imported = profile.get('imported_hero_stats', {}).get(name)
    use_imported = imported is not None and (profile.get('prefer_imported_stats') or prog.get('stars_source') == 'json' or not prog.get('enabled', True))
    return {
        'star_step': int(h.get('star_step', 30)),
        'widget_level': int(h.get('widget_level', 10)),
        'stars_enabled': bool(prog.get('stars_enabled', True)),
        'widget_stats_enabled': bool(prog.get('widgets_enabled', True)),
        'gear_enabled': bool(profile.get('gear_enabled', True)) and not use_imported,
        'gear': copy.deepcopy(profile.get('hero_gear', {}).get(role, default_gear_set())),
        'stats_source': 'imported' if use_imported else 'manual',
        'imported_stats': copy.deepcopy(imported),
        'imported_widget_rank': profile.get('imported_widget_ranks', {}).get(name),
    }

def hero_setting(profile, formation, role, name=None):
    name = name or formation.get('leads', {}).get(role, '')
    if isinstance(name, dict): name = name['name']
    value = default_hero_setting(profile, name, role)
    value.update(copy.deepcopy(formation.get('hero_settings', {}).get(name, {})))
    if profile.get('shared_gear'):
        value['gear']=copy.deepcopy(profile.get('hero_gear',{}).get(role,default_gear_set()))
        value['gear_enabled']=bool(profile.get('gear_enabled_by_role',{}).get(role,True))
        value['widget_level']=int(profile.get('widget_levels',{}).get(role,10))
        value['widget_stats_enabled']=bool(profile.get('widget_stats_by_role',{}).get(role,True))
        # Shared widget level also supplies the active skill rank.
        value['imported_widget_rank']=None
    return value

def active_widget_rank(setting):
    rank = setting.get('imported_widget_rank')
    return int(rank) if setting.get('stats_source') == 'imported' and rank is not None else widget_skill_rank(int(setting.get('widget_level', 0)))

def hero_layers(entry, setting, enabled=True):
    level = int(setting.get('widget_level', 0))
    if setting.get('stats_source') == 'imported':
        total = stat_vector(setting.get('imported_stats') or {})
        zero = {s: 0.0 for s in STAT_NAMES}
        return {'final_hero_stats': total if enabled else zero, 'base_star_stats': total,
                'widget_passive_stats': zero, 'gear_stats': zero,
                'widget_level': level, 'widget_skill_rank': active_widget_rank(setting)}
    return layered_hero_stats(entry, setting, setting.get('gear', {}),
        use_stars=enabled and bool(setting.get('stars_enabled', True)),
        use_passive_widget=enabled and bool(setting.get('widget_stats_enabled', True)),
        use_gear=enabled and bool(setting.get('gear_enabled', True)))

def initialize_formation(profile, formation, *, imported=False):
    toggles = formation.setdefault('add_hero_stats', {})
    settings = formation.setdefault('hero_settings', {})
    if imported: settings.clear()
    for role, name in formation.get('leads', {}).items():
        if isinstance(name, dict): name = name['name']
        value = default_hero_setting(profile, name, role)
        if profile.get('shared_gear'):
            for key in ('gear','gear_enabled','widget_level','widget_stats_enabled'):value.pop(key,None)
        if imported and name in profile.get('imported_hero_stats', {}):
            value.update(stats_source='imported', gear_enabled=False)
            settings[name] = value
            toggles[role] = not profile.get('imported_stats_include_heroes', False)
            if profile.get('imported_specials_included', False): formation.setdefault('widget_buffs', {})[role] = False
        else:
            settings.setdefault(name, value)
            toggles.setdefault(role, bool(profile.get('hero_progression', {}).get('enabled', True)) or
                               (name in profile.get('imported_hero_stats', {}) and not profile.get('imported_stats_include_heroes', False)))

def detach_config(cfg, root, *, read_legacy=False):
    """Only the one-time migration/import may read a player file. Runs never do."""
    from kingshot_progression import load_progression_lookup
    catalog = load_progression_lookup(Path(root) / 'json/kingshot_hero_data.json')
    cfg['lookups'] = {'hero_stats': 'json/kingshot_hero_data.json', 'hero_progression': 'json/kingshot_hero_data.json'}
    for letter, side in [('A', 'attacker'), ('B', 'defender')]:
        profile = cfg['profiles'][letter]
        if read_legacy and not profile.get('input_values_embedded'):
            path = Path(profile.get('player_file', ''))
            if not path.is_absolute(): path = Path(root) / path
            try:
                values = import_player_values(profile, read_player(path), catalog)
                # Do not overwrite stats already edited in the GUI.
                profile['imported_hero_stats'] = values['imported_hero_stats']
                profile['imported_stats_include_heroes'] = values['imported_stats_include_heroes']
                profile['imported_widget_ranks'] = values['imported_widget_ranks']
            except (OSError, ValueError, TypeError):
                if profile.get('hero_progression', {}).get('stars_source') == 'json' or not profile.get('hero_progression', {}).get('enabled', True):
                    cfg.setdefault('migration_notes', []).append(
                        f'{side.title()}: the older player export is unavailable. Saved base stats and manual settings were retained; review hero stats before running.')
        profile['input_values_embedded'] = True
        from kingshot_players import assignment
        for section in ('joiner','lead_troop'):
            assigned_side=next(s for s,l in assignment(cfg[section]).items() if l==letter)
            formations=[cfg[section][assigned_side]] if section=='joiner' else cfg[section][assigned_side]['formations']
            for formation in formations:initialize_formation(profile,formation)
        profile['hero_progression']['stars_source'] = 'manual'
    return cfg

def apply_formation_stats(player, profile, formation, catalog, side):
    """Same calculation as the UI, applied before the simulator import."""
    widgets = {}
    for role, raw_name in formation['leads'].items():
        name = raw_name['name'] if isinstance(raw_name, dict) else raw_name
        entry = catalog[name]
        setting = hero_setting(profile, formation, role, name)
        enabled = formation.get('add_hero_stats', {}).get(role, True)
        calc = hero_layers(entry, setting, enabled)
        sim_name = entry.get('simulator_name', name)
        hero = player['heroes'].get(name, player['heroes'].get(sim_name, {}))
        hero.update(name=sim_name, type=entry['type'], stats=calc['final_hero_stats'],
                    skill_levels={str(k): 5 for k in entry.get('skill_slots', ['1','2','3'])},
                    widget_level=active_widget_rank(setting))
        player['heroes'][name] = hero
        if formation.get('widget_buffs', {}).get(role, True):
            widgets[sim_name] = active_widget_rank(setting)
    player.setdefault('special_bonuses', {})['widgetLevels'] = widgets
    player['stats_include_heroes'] = False
    player['_formation_stats_ready'] = True
    return player

def validate_formation_stats(profile, formation, catalog):
    for role, name in formation.get('leads', {}).items():
        if isinstance(name, dict): name = name['name']
        setting = hero_setting(profile, formation, role, name)
        if setting.get('stats_source') not in ('manual', 'imported'):
            raise ValueError(f'{name}: invalid stat source.')
        if not 0 <= int(setting['star_step']) <= 30 or not 0 <= int(setting['widget_level']) <= 10:
            raise ValueError(f'{name}: invalid stars or exclusive gear level.')
        for slot in GEAR_SLOTS:
            validate_gear_piece(setting.get('gear', {}).get(slot, {}), label=f'{name} {slot}')
        if name in catalog: hero_layers(catalog[name], setting)
