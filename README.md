# Snapchat Memories Organiser

**By [UTSY-YO](https://github.com/UTSY-YO) · 2026**

---

When you export your Snapchat memories, three things go wrong:

1. Filter overlays (stickers, text, drawings) are saved as separate files — not merged onto your photos and videos
2. Every file loses its original date and time — they all appear as "taken today" when you import them
3. GPS location is stored in a separate file and never embedded into the media

This app fixes all three.

---

## What it does

For every memory in your Snapchat export, this app:

- **Merges the filter overlay** back onto the photo or video using Pillow (images) and ffmpeg (videos)
- **Restores the original date and time** by cross-referencing the HTML file in each ZIP against the master JSON — giving a guaranteed correct match even on days where you took many snaps
- **Embeds the GPS location** into the file so it shows correctly on a map in Apple Photos or Google Photos
- **Names each file** using its date and time (`2022-08-06_14-32-11_photo.jpg`) so everything sorts correctly

The result is a folder of properly restored memories, ready to import anywhere.

---

## A note on times

**Dates are always accurate.** Your memories will land on the correct day in any gallery app.

Times are less reliable, especially for older memories (pre-2019). Snapchat changed how it stored timezone data several times over the years, so the hour and minute shown may be off for older snaps. This is a limitation of the original Snapchat export — it can't be corrected.

---

## Requirements

You need Python 3.8 or newer, plus four additional tools. See platform-specific instructions below.

### Windows

```
# Install Python
Download from https://python.org and run the installer.
Tick "Add Python to PATH" during installation.

# Install Python packages
pip install Pillow piexif

# Install ffmpeg (adds ffmpeg and ffprobe)
winget install ffmpeg

# Restart Command Prompt after installing ffmpeg, then verify:
ffmpeg -version
```

### macOS

```
# Python comes with macOS, but for the latest version use:
brew install python     # or download from python.org

# Install Python packages
pip3 install Pillow piexif

# Install ffmpeg via Homebrew (get Homebrew at brew.sh if you don't have it)
brew install ffmpeg

# Verify:
ffmpeg -version
```

### Linux (Ubuntu/Debian)

```
# Install Python and tkinter (tkinter is not always bundled on Linux)
sudo apt update
sudo apt install python3 python3-pip python3-tk

# Install Python packages
pip3 install Pillow piexif

# Install ffmpeg
sudo apt install ffmpeg

# Verify:
ffmpeg -version
```

---

## Running the app

```
python snapchat_organiser_v2.py       # Windows
python3 snapchat_organiser_v2.py      # Mac / Linux
```

No installation needed — just run the script directly.

---

## How to use it

### Step 1 — Request your Snapchat data

1. Go to [accounts.snapchat.com](https://accounts.snapchat.com) and sign in
2. Go to **My Data** → **Submit Request**
3. Make sure **"Export JSON Files for data portability purposes"** is ticked
4. Submit the request — Snapchat will email you a download link (can take minutes to hours)
5. Download **every ZIP file** from the email link (there may be up to 12)

### Step 2 — Keep all ZIPs together

Put all the downloaded ZIPs in one folder — for example, `Snapchat ZIPs` on your Desktop.
**Do not extract them.** Leave them as ZIP files.

### Step 3 — Open the app

Run the script. The **Setup tab** will show whether all dependencies are installed.
If anything shows as Not Installed, run the relevant command and restart the app.

### Step 4 — Add your ZIPs

Go to the **Files tab**:
- Click **Add a folder of ZIPs** and select the folder containing your ZIPs
- Choose an **output folder** (a new empty folder on your Desktop works well)
- Leave the overlay option on unless you want files without filters applied
- Click **Process All ZIPs**

### Step 5 — Monitor progress

The app switches to the **Progress tab** automatically. You'll see:
- Live counts: memories saved, overlays applied, GPS embedded
- Progress bars for the overall run and per-ZIP
- A per-video encoding bar when ffmpeg is active
- Estimated time remaining

**Controls:**
- **Skip video** — kills the current ffmpeg video encode immediately. The original file is saved in its place.
- **Pause** — pauses after the current file finishes. Click Resume to continue.
- **Stop** — halts all processing after the current file. Files already saved are kept.

### Step 6 — Import your memories

Your output folder contains restored media files with correct dates, times, and GPS.
Import into Apple Photos, Google Photos, copy to your phone, upload to a NAS — whatever you prefer.

---

## How matching works

Getting the right timestamp and GPS onto the right file is the hardest part of this problem.

Snapchat stores a master file called `memories_history.json` in your first ZIP. It contains the exact UTC time and GPS for every memory you've ever saved. Each ZIP also contains a `memories_history.html` that lists the media files it contains — in the same order as the JSON.

The app cross-references these:

1. Reads the master JSON to get times and GPS for all memories
2. Parses each ZIP's HTML to get its media files in order
3. Matches each media file to its JSON entry by position — guaranteed correct for every file, even on days with many memories
4. Falls back to matching by date and file type if a ZIP has no HTML

---

## Output filenames

Files are named by their UTC timestamp from the JSON:

```
2019-06-13_04-32-07_photo.jpg
2022-08-06_14-10-22_video.mp4
2022-08-06_14-10-22_video_001.mp4   ← collision handled automatically
```

Apple Photos, Google Photos, and Windows Explorer all display these times converted to your local timezone automatically.

---

## Memories Summary tab

A live breakdown of your memories by year and month, updating as each file is processed:

| Month | Photos | Videos | Total | GPS |
|-------|--------|--------|-------|-----|
| Jan   | 12     | 23     | 35    | 18  |
| Feb   | 45     | 67     | 112   | 54  |
| **Total** | **57** | **90** | **147** | **72** |

GPS counts are shown in green when coordinates were embedded.

---

## Troubleshooting

**"Some dependencies missing" shown at the top**
Run the install commands in the Setup tab and restart the app. The app checks for Pillow, piexif, ffmpeg, and ffprobe each time it starts.

**Videos saved without the filter overlay**
ffmpeg is either not installed or not on your PATH. Install it using the commands above, then restart.

**GPS not embedded on some files**
Snapchat records `0, 0` for memories where location was off or not recorded — these are skipped intentionally. Only memories with real GPS data get embedded coordinates.

**All files show today's date after import**
Make sure you're importing into an app that reads EXIF and MP4 metadata. Google Photos and Apple Photos both do this correctly. Some basic file managers don't.

**A video is taking very long**
Click **Skip video** on the Progress tab. The original file will be saved and the app moves on to the next one. You can also set a timeout (in minutes) on the Files tab so this happens automatically.

**tkinter error on Linux**
Run `sudo apt install python3-tk` and try again.

**Files tab shows no ZIPs after adding a folder**
Make sure the ZIPs haven't been extracted. The app expects `.zip` files, not extracted folders (unless you use the "Add an unzipped folder" option).

---

## Platform notes

| | Windows | macOS | Linux |
|---|---|---|---|
| Creation time set | ✓ (Win32 API) | ✗ (mtime only) | ✗ (mtime only) |
| Modification time set | ✓ | ✓ | ✓ |
| GPS in JPEG | ✓ | ✓ | ✓ |
| GPS in MP4 | ✓ | ✓ | ✓ |
| Filter overlay on photos | ✓ | ✓ | ✓ |
| Filter overlay on videos | ✓ | ✓ | ✓ |

On macOS and Linux, the modification time (not creation time) is set. Apple Photos and Google Photos read modification time when importing, so your memories will still sort correctly.

---

## License

MIT — free to use, modify, and distribute.

---

*Made by [UTSY-YO](https://github.com/UTSY-YO) · 2026*
