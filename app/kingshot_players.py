"""Persistent player identity and gear, with experiment-local battle roles."""
import copy

SIDES=('attacker','defender')
ROLES=('inf','cav','arch')
DEFAULT_ASSIGNMENT={'attacker':'A','defender':'B'}

def assignment(design):
    return copy.deepcopy(design.get('assignment',DEFAULT_ASSIGNMENT))

def migrate_players(cfg):
    legacy=not all(p.get('shared_gear') for p in cfg['profiles'].values())
    cfg.setdefault('player_setup', {letter:copy.deepcopy(cfg['battle_setup'][side]) for side,letter in DEFAULT_ASSIGNMENT.items()})
    for letter,p in cfg['profiles'].items():
        p['name']=str(p.get('name','')).strip() or 'Player '+letter
        if not p.get('shared_gear'):
            side=next(s for s,l in DEFAULT_ASSIGNMENT.items() if l==letter)
            candidates={r:[] for r in ROLES}
            for section in ('joiner','lead_troop'):
                items=[cfg[section],*cfg.get('experiments',{}).get(section,[])]
                for item in items:
                    forms=[item[side]] if section=='joiner' else item[side]['formations']
                    for form in forms:
                        for r,name in form.get('leads',{}).items():
                            setting=form.get('hero_settings',{}).get(name,{})
                            if 'gear' in setting:candidates[r].append(setting)
            p['widget_levels']={r:10 for r in ROLES}
            p['gear_enabled_by_role']={r:bool(p.get('gear_enabled',True)) for r in ROLES}
            p['widget_stats_by_role']={r:True for r in ROLES}
            for r,values in candidates.items():
                changed=[v for v in values if v.get('gear')!=p.get('hero_gear',{}).get(r)]
                chosen=(changed or values or [None])[0]
                if chosen:
                    p['hero_gear'][r]=copy.deepcopy(chosen['gear'])
                    p['gear_enabled_by_role'][r]=bool(chosen.get('gear_enabled',True))
                    p['widget_levels'][r]=int(chosen.get('widget_level',10))
                    p['widget_stats_by_role'][r]=bool(chosen.get('widget_stats_enabled',True))
                    if any(v.get('gear')!=chosen['gear'] for v in changed):
                        note=f"{p['name']}: older formations used different {r} gear. The first edited set is now shared; review Gear / Widgets on page 1."
                        if note not in cfg.setdefault('migration_notes',[]):cfg['migration_notes'].append(note)
            p['shared_gear']=True
    for section in ('joiner','lead_troop'):
        for item in [cfg[section],*cfg.get('experiments',{}).get(section,[])]:
            item.setdefault('assignment',copy.deepcopy(DEFAULT_ASSIGNMENT))
            for side in SIDES:
                forms=[item[side]] if section=='joiner' else item[side]['formations']
                for form in forms:
                    for value in form.get('hero_settings',{}).values():
                        for key in ('gear','gear_enabled','widget_level','widget_stats_enabled'):value.pop(key,None)
    return cfg

def player_names(cfg,section=None,design=None):
    roles=assignment(design if design is not None else cfg.get(section,{})) if section or design is not None else cfg.get('assignment',DEFAULT_ASSIGNMENT)
    return {side:cfg['profiles'][letter].get('name') or 'Player '+letter for side,letter in roles.items()}
