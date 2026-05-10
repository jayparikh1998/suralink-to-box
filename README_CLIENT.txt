Suralink Box Sync - Windows Client Instructions
===============================================

1. Unzip the folder you were given.

2. Place your .env file beside SuralinkBoxSync.exe.

3. Place your Box JWT config JSON file beside SuralinkBoxSync.exe.
   The expected filename is box_config.json unless your .env uses a different
   BOX_JWT_CONFIG_PATH value.

4. Double-click SuralinkBoxSync.exe.

5. A terminal window will open, then the app will open automatically in your
   browser at http://localhost:8501.

6. To stop the app, close the terminal window.

Required files in the same folder:
- SuralinkBoxSync.exe
- .env
- box_config.json

Example .env setting for the Box config:
BOX_JWT_CONFIG_PATH=box_config.json

Do not email or share .env or box_config.json unless your organization has
approved that method for sharing credentials.
