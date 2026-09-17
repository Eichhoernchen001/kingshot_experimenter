# Kingshot Experiment Manager — v1.23

## Windows

1. Extract the entire package or clone the repository. Keep the folders together.
2. Install Python 3.10 or newer with Tcl/Tk support from https://www.python.org/downloads/ (enable Add Python to PATH).
3. Double-click `SETUP.bat` and wait for Setup complete. Anaconda users can also run it from Anaconda Prompt.
4. Double-click `START.bat` to open the app.

Setup creates `.venv` and installs dependencies and Playwright Chromium. These are generated locally and are not included in Git. Setup and simulations need internet access. Battles use https://kingshotsimulator.com/battle/. Existing results can be analyzed offline.

## macOS

Use macOS 14 or newer with a standard Python 3.10+ installation including Tkinter. In Terminal, change to this folder, then run `bash SETUP.command` once and `bash START.command` to launch. Mac support has been checked in code, not on an actual Mac.

## Configure your experiment

This clean distribution contains no personal player exports, saved configuration, results, or Python environment. It starts with the built-in example formations and default settings; base player stats start at zero. Enter your own stats before running.

On page 1 name Player A and Player B and set their base stats, gear, widgets and bonuses. To import a simulator player export, select it with Browse and click Use .json stats. The default player paths are placeholders; those files are not required when values are entered manually. Review all settings, including default equipment, before running.

Pages 2 and 3 assign attacker/defender roles and configure lead + troop or joiner experiments. Stars and active widget buffs are selected beside the heroes. Add experiments to run a sequence; Run all runs both experiment types. The varied side determines the reported win chance.

Use Run & analyze to validate, preview and start your experiment, then generate plots. Results and run settings are saved under `results/`. Import from previous restores an earlier experiment. Local configuration is saved as `kingshot_config.json`; both it and results are ignored by Git.

## Joiner modes

Complete tests the full configured set. Fast and Faster use adaptive screening, copy probes, refinement and independent validation. Faster can additionally stop when the ranking remains stable. Set budgets, copy limits and an optional comma-separated Heroes to test first list in Accelerated settings. This list prioritizes checks without assuming heroes are strong or weak.

Promising heroes can receive duplicate, triple and quadruple tests within the configured limits. Strong heroes may be provisionally fixed in some tests, while challenge teams continue to test alternatives. Decisions can be reversed. Accelerated modes can miss unusual synergies; their uncertainty estimates are approximate. Validation reports prediction error and separates predicted recommendations from observed validation results. Unidentifiable synergy effects are omitted with a notice.

Start in a new results folder to use the v1.23 planner. Old checkpoints resume with their original planner. Do not resume an experiment after changing its battle inputs.

## Files

- `app/`: application modules and `requirements.txt` (the dependency list used by setup).
- `json/`: required hero data, gear data and the neutral player template.
- `SETUP.*` and `START.*`: Windows and macOS launchers.
- `.gitignore`: excludes generated environments, caches, results and local settings.

## Troubleshooting

Rerun setup and read its error message. Missing Tkinter requires a Python installation with Tcl/Tk. On macOS, certificate errors may require the Python installer’s Install Certificates.command. On Windows, run `.venv\Scripts\python.exe app\kingshot_gui.py` from Command Prompt in this folder to see startup errors.

After moving the app to another folder or computer, run setup in a fresh copy to recreate the environment. Keep your existing testing folder and results separate when updating this clean distribution.
