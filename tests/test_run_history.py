import copy
import json
from pathlib import Path
import sys
import shutil
import uuid
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from kingshot_config import default_config, save_config, apply_config_to_globals
from kingshot_run_history import result_folder, write_snapshot, previous_runs, import_run, configured_result_folder, automatic_result_name, DEFAULT_FOLDER_LABEL


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / ('history_test_' + uuid.uuid4().hex)
        self.root.mkdir()
        assert self.root.resolve().is_relative_to(Path.cwd().resolve())
        self.addCleanup(shutil.rmtree, self.root)
        self.cfg = default_config()
        for value in [p['player_file'] for p in self.cfg['profiles'].values()] + list(self.cfg['lookups'].values()):
            p = self.root / value
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{}', encoding='utf-8')

    def test_snapshot_roundtrip_both_types_and_scope(self):
        for section in ('lead_troop', 'joiner'):
            folder = result_folder(self.root / 'results', section, 'Custom run')
            cfg = copy.deepcopy(self.cfg)
            cfg['run']['batches_per_condition'] = 17
            cfg['profiles']['A']['name'] = 'Raist'
            cfg['profiles']['B']['name'] = 'Earth'
            cfg['profiles']['A']['base_stats']['inf']['attack'] = 123
            write_snapshot(folder, cfg, section, self.root)
            current = copy.deepcopy(self.cfg)
            other = 'joiner' if section == 'lead_troop' else 'lead_troop'
            current[other]['sentinel'] = True
            loaded, notes = import_run(folder, section, current, self.root)
            self.assertFalse(notes)
            self.assertEqual(loaded[section], cfg[section])
            self.assertEqual(loaded[other], current[other])
            self.assertEqual(loaded['run']['batches_per_condition'], 17)
            self.assertEqual(loaded['profiles']['A']['name'], 'Raist')
            self.assertEqual(loaded['profiles']['B']['name'], 'Earth')
            self.assertEqual(loaded['profiles']['A']['base_stats']['inf']['attack'], 123)
            self.assertTrue(Path(loaded['profiles']['A']['player_file']).is_file())
            self.assertEqual(previous_runs(self.root / 'results', section), [folder])
            with self.assertRaises(ValueError):
                import_run(folder, other, current, self.root)

    def test_folder_validation_and_defaults(self):
        base = self.root / 'results'
        self.assertEqual(result_folder(base, 'joiner'), base / 'joiner_experiment')
        for name in ('../other', 'a/b', 'C:\\out', 'CON', 'NUL.txt', 'bad.', '..'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                result_folder(base, 'joiner', name)

    def test_script_paths_follow_each_name(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['result_names'] = {'lead_troop': 'Lead run', 'joiner': 'Joiner run'}
        save_config(self.root, cfg)
        for section in cfg['result_names']:
            target = {'SCRIPT_FOLDER': self.root}
            apply_config_to_globals(target, section)
            expected = result_folder(self.root / 'results', section, cfg['result_names'][section])
            for key in ('OUTPUT_CSV', 'SETTINGS_TXT', 'FIRST_ATTACKER_JSON', 'FIRST_DEFENDER_JSON'):
                self.assertEqual(target[key].parent, expected)

    def test_legacy_literals_are_data_and_missing_values_preserved(self):
        folder = result_folder(self.root / 'results', 'joiner')
        folder.mkdir(parents=True)
        (folder / 'experiment_settings.txt').write_text(
            'SIMULATIONS_PER_BATCH = 200\njoiner_pool_atk_max_3 = [\n  "Yang"\n]\njoiner1_def = []\n', encoding='utf-8')
        loaded, notes = import_run(folder, 'joiner', self.cfg, self.root)
        self.assertEqual(loaded['run']['simulations_per_batch'], 200)
        self.assertEqual(loaded['joiner']['attacker']['pools']['3'], ['Yang'])
        self.assertEqual(loaded['profiles'], self.cfg['profiles'])
        self.assertTrue(notes)
        (folder / 'experiment_settings.txt').write_text("SIMULATIONS_PER_BATCH = __import__('os').getcwd()", encoding='utf-8')
        with self.assertRaises(ValueError):
            import_run(folder, 'joiner', self.cfg, self.root)

    def test_gui_startup_defaults_and_workflow_paths(self):
        import kingshot_gui as gui
        cfg = copy.deepcopy(self.cfg)
        cfg['result_names'] = {'joiner': 'Last run', 'lead_troop': 'Old lead run'}
        with patch.object(gui, 'load_config', return_value=cfg):
            app = gui.KingshotApp()
        app.withdraw()
        self.addCleanup(app.destroy)
        self.assertEqual({k: v.get() for k, v in app.run_tab.result_names.items()}, {'joiner': '', 'lead_troop': ''})
        app.run_tab.result_names['joiner'].set('New joiner')
        app.run_tab.result_names['lead_troop'].set('New lead')
        with patch.object(app, 'run_script') as run:
            app.run_tab.analyze()
            self.assertEqual(Path(run.call_args.args[1][0]).parent.name, 'New joiner')
            app.run_tab.plot_results()
            self.assertEqual(Path(run.call_args.args[1][-1]).name, 'New lead')
        committed = app.commit_ui()
        self.assertEqual(committed['result_names']['joiner'], 'New joiner')

    def test_gui_picker_imports_selected_run(self):
        import kingshot_gui as gui
        saved = copy.deepcopy(self.cfg)
        saved['run']['batches_per_condition'] = 23
        folder = result_folder(self.root / 'results', 'joiner', 'Saved joiners')
        write_snapshot(folder, saved, 'joiner', self.root)
        app = gui.KingshotApp(); app.withdraw()
        self.addCleanup(app.destroy)
        with patch.object(gui, 'RESULTS_DIR', self.root / 'results'):
            app.import_previous('joiner')
            dialog = next(w for w in app.winfo_children() if isinstance(w, gui.tk.Toplevel))
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            button = next(w for w in descendants(dialog) if isinstance(w, gui.ttk.Button) and w.cget('text') == 'Import')
            with patch.object(gui.messagebox, 'showerror') as error:
                button.invoke()
                error.assert_not_called()
            self.assertEqual(app.config_data['run']['batches_per_condition'], 23)
            self.assertEqual(app.run_tab.result_names['joiner'].get(), 'Saved joiners')

    def test_automatic_player_folders_and_overrides(self):
        self.cfg['profiles']['A']['name'] = 'Raist'
        self.cfg['profiles']['B']['name'] = 'Earth'
        self.cfg['joiner']['attacker']['troop_percentages'] = [50, 50, 0]
        self.cfg['joiner']['defender']['troop_percentages'] = [60, 10, 40]
        self.assertEqual(automatic_result_name(self.cfg, 'joiner'), 'Raist_50-50-0_vs_Earth_60-10-40')
        self.assertEqual(automatic_result_name(self.cfg, 'lead_troop'), 'Raist_vs_Earth')
        folder = configured_result_folder(self.root / 'results', self.cfg, 'joiner')
        self.assertEqual(folder.name, 'Raist_50-50-0_vs_Earth_60-10-40')
        self.cfg['result_names'] = {'joiner': 'Manual name'}
        self.assertEqual(configured_result_folder(self.root, self.cfg, 'joiner').name, 'Manual name')
        self.cfg['result_names']['joiner'] = DEFAULT_FOLDER_LABEL
        self.assertEqual(configured_result_folder(self.root, self.cfg, 'joiner'), self.root / 'joiner_experiment')
        self.cfg['profiles']['A']['name'] = 'A/B: C'
        self.assertEqual(automatic_result_name(self.cfg, 'lead_troop'), 'A_B_C_vs_Earth')

    def test_live_gui_names_determine_folder_before_saving(self):
        import kingshot_gui as gui
        app = gui.KingshotApp(); app.withdraw()
        self.addCleanup(app.destroy)
        app.profiles_tab.player_a.player_name.set('Raist')
        app.profiles_tab.player_b.player_name.set('Earth')
        self.assertEqual(app.run_tab.output_folder('lead_troop').name, 'Raist_vs_Earth')
        self.assertTrue(app.run_tab.output_folder('joiner').name.startswith('Raist_'))
        self.assertIn('_vs_Earth_', app.run_tab.output_folder('joiner').name)
        self.assertEqual(app.commit_ui()['profiles']['B']['name'], 'Earth')

    def test_whole_profiles_page_swap_and_import_buttons(self):
        import kingshot_gui as gui
        app = gui.KingshotApp(); app.withdraw()
        self.addCleanup(app.destroy)
        tab = app.profiles_tab
        tab.player_a.player_name.set('Raist')
        tab.player_b.player_name.set('Earth')
        tab.atk_setup.total.set('123000')
        tab.def_setup.total.set('456000')
        tab.atk_setup.quality_vars[('inf','tier')].set('10')
        original = copy.deepcopy(app.commit_ui())
        with patch.object(tab, 'refresh_displays', wraps=tab.refresh_displays) as refreshed:
            tab.swap_players()
            refreshed.assert_called_once()
        swapped = app.commit_ui()
        self.assertEqual(swapped['profiles']['A'], original['profiles']['B'])
        self.assertEqual(swapped['profiles']['B'], original['profiles']['A'])
        self.assertEqual(swapped['battle_setup']['attacker'], original['battle_setup']['defender'])
        self.assertEqual(swapped['battle_setup']['defender'], original['battle_setup']['attacker'])
        for section in ('lead_troop', 'joiner', 'run'):
            self.assertEqual(swapped[section], original[section])
        self.assertEqual(app.run_tab.output_folder('lead_troop').name, 'Earth_vs_Raist')
        tab.swap_players()
        restored = app.commit_ui()
        self.assertEqual(restored['profiles'], original['profiles'])
        self.assertEqual(restored['battle_setup'], original['battle_setup'])
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        buttons = {w.cget('text'):w for w in descendants(tab) if isinstance(w, gui.ttk.Button)}
        self.assertNotIn('Import from previous...', buttons)
        with patch.object(app, 'import_previous') as importer:
            buttons['Lead + Troops'].invoke()
            importer.assert_called_with('lead_troop')
            buttons['Joiner'].invoke()
            importer.assert_called_with('joiner')
        # Invalid input must not partially exchange valid player settings.
        tab.atk_setup.total.set('invalid')
        with patch.object(gui.messagebox, 'showerror') as error:
            tab.swap_players()
            error.assert_called_once()
        self.assertEqual(tab.current_profiles(), original['profiles'])

    def test_names_saved_in_both_experiment_metadata_files(self):
        import kingshot_joiner_experiment as joiner
        import kingshot_lead_troop_experiment as lead
        self.cfg['profiles']['A']['name'] = 'Raist'
        self.cfg['profiles']['B']['name'] = 'Earth'
        for module in (joiner, lead):
            path = self.root / 'settings.json'
            path.write_text('{}', encoding='utf-8')
            with patch.object(module, 'ACTIVE_KINGSHOT_CONFIG', self.cfg):
                if module is joiner:
                    with patch.object(module, '_load_json', return_value={}):
                        module._augment_machine_settings(path)
                else:
                    module._augment_machine_settings(path, {}, {})
            self.assertEqual(json.loads(path.read_text())['player_names'], {'attacker': 'Raist', 'defender': 'Earth'})

    def test_plot_names_ratios_and_both_legend_sides(self):
        import kingshot_full_model_analysis as m
        import kingshot_lead_troop_plotter as lead_plot
        import numpy as np
        import pandas as pd
        from types import SimpleNamespace
        data = {'player_names': {'attacker': 'Raist', 'defender': 'Earth'}, 'joiner_experiment': {
            'attacker_lead_heroes': ['Triton', 'Thrud', 'Marlin'], 'defender_lead_heroes': ['Triton', 'Sophia', 'Vivian'],
            'attacker_troop_percentages': {'infantry': 50, 'cavalry': 50, 'archers': 0},
            'defender_troop_percentages': {'infantry': 60, 'cavalry': 10, 'archers': 40}}}
        lines = m.prediction_plot_context_lines([(self.root, self.root, data)], None, None, 'attacker')
        self.assertEqual(lines[0], 'Raist (attacker): Triton/Thrud/Marlin - 50-50-0')
        self.assertEqual(lines[1], 'Earth (defender): Triton/Sophia/Vivian - 60-10-40')
        self.assertEqual(m.prediction_plot_context_lines([(self.root,self.root,data)],None,None,'defender'), lines[::-1])
        path = self.root / 'kingshot_lead_troop_winrates_experiment_settings.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(lead_plot.player_matchup_title(self.root), 'Raist (attacker) vs Earth (defender)')
        cond = pd.DataFrame({'lineup_key': [('A','A','B','B'), ('A','B','B','B')], 'p_observed': [.41,.49], 'trials': [100,100]})
        fit = SimpleNamespace(prediction=np.array([.4,.5]), covariance=np.eye(2)*.001)
        for side in ('attacker','defender'):
            with patch.object(m.plt.Figure, 'savefig'), patch.object(m.plt, 'close'):
                m.plot_prediction(cond,['A','B'],pd.DataFrame(np.eye(2)),fit,self.root,2,'plot.png','test',lines,100,side)
                figure = m.plt.gcf()
                self.assertIn(f'Top 2 {side} joiner lineups', figure.axes[0].get_legend().get_title().get_text())
            m.plt.close(figure)


if __name__ == '__main__':
    unittest.main()
