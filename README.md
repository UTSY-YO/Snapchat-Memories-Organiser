# Snapchat Memories Organiser

**Made by [UTSY-YO](https://github.com/UTSY-YO) · April 2026**

A Python desktop app that helps you take back control of your Snapchat memories. As Snapchat increasingly moves storage behind paid services, this tool makes it easy to preserve your memories locally and migrate them to your own devices or cloud platforms.

---

## The Problem

When you request your Snapchat Memories export, Snapchat provides ZIP files containing raw media and separate overlay assets. This creates several problems:

- Photos and videos are exported **without Snapchat text, stickers, drawings, or GIF overlays**
- Overlay elements are stored **separately** from the original media
- The **original creation date and time is missing** from the files themselves
- Importing into Apple Photos, Google Photos, or any gallery app makes everything appear as if it was **created today**

---

## What This App Does

Snapchat Memories Organiser automatically processes your exported Snapchat data to fix all of the above:

- **Merges overlays** back onto photos and videos (filters, stickers, text)
- **Restores original timestamps** from the master JSON export file
- **Embeds GPS coordinates** where available
- **Organises output files** with clean, date-based filenames
- **Live progress tracking** with per-file stats, ETA, and a memories summary by year and month

---

## Requirements

### Python
Python 3.9 or newer — [python.org](https://www.python.org/downloads/)

### Python packages
```
pip install Pillow piexif
```

### ffmpeg + ffprobe
Required for video overlay compositing and MP4 metadata embedding.

| Platform | Install |
|----------|---------|
| Windows  | `winget install ffmpeg` |
| macOS    | `brew install ffmpeg` |
| Linux    | `sudo apt install ffmpeg` |

After installing ffmpeg, restart the app so it is detected on your PATH.

---

## Installation

1. Download `snapchat_organiser_v2.py`
2. Install dependencies (see above)
3. Run:

```bash
python snapchat_organiser_v2.py
```

No installation required — it runs directly from the script.

---

## How to Use

### Part 1 — Downloading Your Data from Snapchat

> ⏳ Snapchat can take **up to 24 hours** to prepare your export. Do this first and come back once the email arrives.

**Step 1 — Request your export**
1. Go to [accounts.snapchat.com](https://accounts.snapchat.com) and sign in, then click **My Data**
2. Under *Select data to include*, tick **both** of the following:
   - ✅ **Export your Memories**
   - ✅ **Export JSON Files** *(for data portability purposes)*
3. Click **Next**
4. You will be prompted to select a **date range** — choose **All Time** so every memory is included
5. **Confirm your email address** in the field provided
6. Click **Submit**

**Step 2 — Wait for the email**

Snapchat will send an email from *Team Snapchat* with the subject **"Your Snapchat data is ready for download"**. Click the link inside — it will direct you back to [accounts.snapchat.com](https://accounts.snapchat.com) → **My Data**.

**Step 3 — Download all ZIP files**
1. Under **Your exports**, click **See exports**
2. You will see a list of ZIP files (e.g. `mydata~....zip`, `mydata~...-2.zip`, up to 12 files, each up to 2 GB)
3. Download **every ZIP file** to your **Downloads** folder
4. Once all ZIPs are downloaded, create a new folder (e.g. `Snapchat ZIPs` on your Desktop) and move all the ZIPs into it — **do not extract them**

> 💡 Make sure you have enough free disk space before downloading. The full export can exceed 20 GB.

---

### Part 2 — Processing with the App

**Step 4 — Open the app and go to the Files tab**
- Click **Add a folder of ZIPs** and select your ZIPs folder
- Choose an output folder where your organised memories will be saved
- Adjust options if needed (overlays, ffmpeg timeout)
- Click **Process All ZIPs**

**Step 5 — Wait for processing**

Switch to the **Progress tab** to monitor live stats, progress bars, and ETA. Use the **Skip** button to jump past any video taking too long.

**Step 6 — Import your memories**

Your output folder contains properly restored media — import into Apple Photos, Google Photos, Android galleries, NAS storage, or anywhere you choose.

---

## ZIP Structure

Snapchat exports your data across multiple ZIPs:

```
mydata.zip          ← ZIP 1: contains HTML + JSON master files + 2016–2018 media
mydata-2.zip        ← ZIPs 2–12: media files only
mydata-3.zip
...
mydata-12.zip
```

The master `memories_history.json` in ZIP 1 covers **all memories across all ZIPs**. The app pre-scans everything before touching any media.

---

## How Matching Works

Getting the right timestamp and GPS onto the right file is the hardest part of this problem. Here is exactly how it is done:

1. **Pre-scan** — all ZIPs are scanned to find `memories_history.json`. This master file contains exact UTC times and GPS for every memory across all ZIPs.

2. **HTML parsing** — each ZIP contains a `memories_history.html` listing its media files by UUID filename in document order.

3. **Cross-reference** — each UUID from the HTML is matched to its position in the master JSON. Both are in the same chronological order, so `HTML entry N = JSON entry N = correct time + GPS`. This is guaranteed correct for every file, even on days where many memories were taken.

4. **Fallback** — if no HTML is found in a ZIP, positional matching by date and media type is used instead.

---

## Output Filenames

Files are named using their UTC timestamp from the JSON:

```
2019-06-13_04-32-07_photo.jpg
2019-06-13_06-15-22_video.mp4
2019-06-13_06-15-22_video_001.mp4   ← collision handled automatically
```

Windows Explorer, Google Photos, and Apple Photos all convert UTC to your local timezone for display automatically.

---

## Note on Timestamps

**Dates are accurate for all years.**

Times (UTC) may be inconsistent for some memories — Snapchat has changed how it stored timezones over time and early exports did not always record times accurately. This is a Snapchat export limitation, not a bug in this app.

---

## Memories Summary Tab

A live breakdown of your memories by year and month, updating as each file is processed:

| Month | Photos | Videos | Total | GPS |
|-------|--------|--------|-------|-----|
| Jan   | 12     | 23     | 35    | 18  |
| Feb   | 45     | 67     | 112   | 54  |
| **Total** | **312** | **535** | **847** | **201** |

GPS counts are highlighted in green when coordinates were successfully embedded.

---

## Options

| Option | Default | Description |
|--------|---------|-------------|
| Apply overlays | On | Composite Snapchat filter overlays onto photos and videos. Disable to run faster. |
| ffmpeg timeout | 10 min | Kill and skip any video that takes longer than this to encode. Set to 0 for no limit. |

---

## Troubleshooting

**"Some dependencies missing" in the header**
Install the missing packages shown on the Setup tab and restart the app.

**Videos saved without overlay**
ffmpeg is not installed or not on your PATH — see Requirements above.

**GPS not embedded for some files**
Snapchat records `0.0, 0.0` for memories where location was off or unavailable. These are intentionally skipped.

**Files show today's date after import**
Make sure you are importing into an app that reads EXIF/MP4 metadata (Google Photos, Apple Photos). Some apps ignore embedded timestamps.

**Processing seems stuck on a video**
Use the **Skip** button on the Progress tab to kill the current encode and move on.

---

## License

MIT — free to use, modify, and distribute.

---

*Made by [UTSY-YO](https://github.com/UTSY-YO) · April 2026*
