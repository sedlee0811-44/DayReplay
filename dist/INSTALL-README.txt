=============================================================================
                          DayReplay - Installation
                   A diary that writes itself (v1.1.1)
=============================================================================

THANK YOU for downloading DayReplay!

This file is your offline installation guide. No internet needed to read this,
but you WILL need internet once (for downloading AI models, ~1.3 GB).


QUICK INSTALL (3 steps)
-----------------------

Step 1: EXTRACT
  Right-click the ZIP you downloaded → "Extract All..." → pick a folder.
  Example: C:\Users\YourName\DayReplay\

  [TIP] Do NOT put it in "Program Files" or any system folder.
        Your own user folder is perfect.

Step 2: DOUBLE-CLICK TO INSTALL
  Open the extracted folder.
  Find and double-click:   install.bat
  
  A black window will pop up and do everything automatically:
  - Create a desktop shortcut
  - Check if AI models are downloaded
  - Offer to download models if missing
  - Run a quick self-test

Step 3: START USING
  Look at your Desktop. You should see:
  
    DayReplay          → double-click to START recording
    Stop DayReplay     → double-click to STOP recording
  
  That's it! Within minutes, open your first daily timeline.


WHAT GETS INSTALLED WHERE?
--------------------------

  Your Folder\
  ├── install.bat           ← you just ran this
  ├── start_voice.vbs       ← starts recording (double-click or shortcut)
  ├── stop_voice.vbs        ← stops recording
  ├── open_ledger.py        ← opens today's timeline page
  ├── python\               ← built-in Python (don't need to install Python!)
  ├── models\               ← AI models (~1.3 GB, downloaded on demand)
  ├── data\                 ← your recordings, timelines, database
  │   └── *.db              ← SQLite database (all your data is here)
  └── logs\                 ← log files (for troubleshooting)


NO ADMIN RIGHTS NEEDED
----------------------

DayReplay runs entirely from your user folder.
No administrator password. No system changes. No Windows services.
To uninstall: just delete the folder and the desktop shortcuts.


FIRST RUN - MODEL DOWNLOAD
---------------------------

When you run install.bat for the first time, it will ask about AI models.

These models are what let DayReplay understand:
  - Your speech (speech-to-text)
  - Sounds around you (footsteps, doors, water, etc.)
  - Who is speaking (speaker recognition)

Size: ~1.3 GB total
Source: HuggingFace (free, open-source)
Time: depends on your internet speed (5-30 min typical)

After download, DayReplay works 100% OFFLINE forever.

[OPTIONAL] If you have NO internet on the target machine:
  Download the "full" package (~3.4 GB) which includes models.
  Request it here: https://github.com/sedlee0811-44/DayReplay/issues


TROUBLESHOOTING
---------------

Problem: "Python runtime not found"
  Fix: You didn't extract the full ZIP. Re-download and extract everything,
       keeping the folder structure intact.

Problem: Model download fails
  Fix: Run this manually later:
         cd (your DayReplay folder)
         python\python.exe download_models.py
       If your network blocks HuggingFace, try setting a mirror:
         set HF_ENDPOINT=https://hf-mirror.com
         python\python.exe download_models.py

Problem: Shortcut doesn't appear on Desktop
  Fix: Manually create one:
       1. Right-click start_voice.vbs → Send to → Desktop (create shortcut)
       2. Do the same for stop_voice.vbs

Problem: Self-test fails
  Fix: Check logs\daemon.log for details.
       Most issues are fixed by re-running install.bat.


UPDATING
--------

When a new version is released:

  1. Download the new ZIP from:
     https://github.com/sedlee0811-44/DayReplay/releases

  2. Extract to a NEW folder (don't overwrite!)

  3. Copy these from your OLD folder to the NEW folder:
       - data\      (your recordings & database)
       - models\    (AI models, if you downloaded them)

  4. Run install.bat in the new folder.

  5. If everything works, delete the old folder.


SUPPORT
-------

  - Issues / bug reports: https://github.com/sedlee0811-44/DayReplay/issues
  - Discussions / questions: https://github.com/sedlee0811-44/DayReplay/discussions
  - README (full docs): https://github.com/sedlee0811-44/DayReplay#readme


Thank you for trying DayReplay! 🎙️

=============================================================================
