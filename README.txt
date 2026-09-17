KINGSHOT EXPERIMENT MANAGER - v1.22

GETTING STARTED ON WINDOWS

1. Extract the entire ZIP. Keep all files and subfolders together.
2. Install Python 3.10 or newer from https://www.python.org/downloads/
   Use the standard installer with Tcl/Tk support.
   Enable "Add Python to PATH" if offered.
3. Double-click SETUP.bat once and wait for "Setup complete."
4. Double-click START.bat whenever you want to open the app.

If you use Anaconda, you can run SETUP.bat from Anaconda Prompt.

GETTING STARTED ON MAC

Use macOS 14 or newer and a standard Python 3.10+ installation from
https://www.python.org/downloads/macos/ with Tkinter included.
Mac support has been checked in the code, but not on an actual Mac.

1. Extract the ZIP.
2. In Terminal, type cd followed by a space, drag the extracted project
   folder into the window, and press Return.
3. Run: bash SETUP.command
4. Run: bash START.command

Setup is needed once; use START.command for later launches.
You can also double-click the .command files if macOS allows them to open.
Mac users use .command files; Windows users use .bat files.

USING THE APP

1. Players, gear & bonuses: name Player A/B and set shared equipment.
2. Lead + troops / 3. Joiners: choose your experiment settings.
4. Run & analyze: validate the configuration, preview, then run.
   Create plots when the results are ready.

Player roles are chosen separately for each experiment on pages 2 and 3.
Gear / Widgets on page 1 sets shared Infantry, Cavalry and Archer gear.
Stars and active widget buffs remain beside each selected hero.

Joiner modes on page 3: Complete, Fast and Faster. Complete stays the
default. Accelerated settings control the screening/refinement/validation
budgets, shortlist and copy limits. See ACCELERATED_JOINERS.txt for details.

Settings are saved in kingshot_config.json. You normally edit them in the
app, not in a text editor. "Use last saved settings" reloads the saved
settings for the selected page.

FOLDERS

app     - Program files. Leave these together; no need to open them.
json    - Player A/B presets and the required hero lookup data.
results - Your generated experiment results and plots.

You can put your own player exports in json and select them on page 1.
Do not delete the hero lookup files. The supplied player files are starting
presets, not automatically fetched account data.

Setup creates a private .venv folder and downloads the required Python
packages and a separate Chromium browser. Do not share that environment.
Simulations require internet access to https://kingshotsimulator.com/battle/
Analysis of existing results can run offline.

UPDATING FROM AN OLDER VERSION

Extract this version into a new folder and run its setup.
Back up your old settings, player JSONs, and results before updating.
You can copy your previous kingshot_config.json into the new main folder
and your previous playerA_data.json/playerB_data.json into json.
The app recognizes their old standard file paths automatically. If you
used other filenames or locations, select those exports again on page 1.
Use Save configuration to save the updated paths.

To keep old results, copy your old lead_troop_experiment and/or
joiner_experiment folders into the new results folder. Avoid overwriting
any newer results.

IF SOMETHING DOES NOT START

Rerun setup and read its error message. Setup and simulations need internet.
Missing Tkinter: install a standard Python with Tcl/Tk support.
Mac certificate error: run the Python installer's Install Certificates.command.
Windows startup closes immediately: open Command Prompt in this folder and
run .venv\Scripts\python.exe app\kingshot_gui.py to see the error.

Keep the project in the same location after setup. If you move it to a new
location or computer, create a fresh copy and run setup there.


See LEAD_TROOP_PLOTS.txt for v1.22 plot/order and player-assignment fixes.
