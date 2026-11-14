# Split-screen library import folder

Put video files (`.mp4`, `.mov`, `.webm`, `.mkv`) here. The API imports them
into the self-host split-screen library when it starts and when `/library` is
used in the Telegram bot (`POST /api/slot-uploads/import`). A valid file is
moved into the library under its file name without the extension; an invalid
one stays here. Subfolders and symlinks are ignored. Everything here except
this README is git-ignored. Another folder can be set with
`SLOT_LIBRARY_IMPORT_DIR`.
