# FileRecall — User Guide

## 📖 Overview

**FileRecall** is a smart review tool based on spaced repetition. It helps you manage and review study materials efficiently by scheduling reviews at intervals that improve long-term retention.

### 🔍 Features

- **Spaced repetition**: Uses 1-, 2-, 4-, 7-, 15-, and 30-day intervals to schedule reviews
- **File management**: Add individual files or whole folders
- **Daily reminders**: Get notified at a fixed time each day
- **Native macOS UI**: Simple, familiar interface
- **Auto-scan**: Periodically scans linked folders and adds new files
- **Smart filtering**: Skips system and temporary files automatically

## 🚀 Installation

### Option 1: Run from source (development)

1. **Requirements**: Python 3.7+ and PyObjC
2. **Install dependencies**:
   ```bash
   pip install pyobjc
   ```
3. **Run**:
   ```bash
   python learning_review.py
   ```

### Option 2: Build app bundle (recommended)

1. **Install py2app**:
   ```bash
   pip install py2app
   ```
2. **Build**:
   ```bash
   python setup.py py2app
   ```
3. **Run**: Open the `.app` from the `dist` folder

## 📚 How to use

### First run

1. **Launch**: On first run, the Settings tab opens
2. **Add content**:
   - **Add Folder**: Choose a folder with your study files
   - **Add File**: Choose individual files
3. **Set reminder time**: Default is 9:00 AM; change it in Settings
4. **Save**: Click **Save & Update Schedule**

### Daily use

1. **Reminders**: You’ll get a notification at the time you set
2. **Today’s list**: Open the app or tap the notification to see files due today
3. **Review**:
   - **Double-click** a file to open it and mark it as reviewed
   - **Batch open**: Command-click to select multiple files, then click **Open Selected**
   - **Show in Finder**: Click the magnifier icon next to a file to reveal it in Finder
4. **Progress**: After you open a file, the app updates the next review date using the spaced-interval schedule

### Settings

1. **Change reminder time**: Adjust hour/minute in Settings and click **Save & Update Schedule**
2. **Manage folders**: View, add, or remove linked folders in Settings
3. **Run now**: Click **Run Now** to run one review check manually
4. **Language**: Use the **Language** dropdown to switch between **System**, **简体中文**, and **English**
5. **Reset**: Use **Reset All Data** to clear all folders and review history (cannot be undone)

## 🎯 Details

### Spaced repetition

Reviews are scheduled along a simple curve:

- 1st review: 1 day later  
- 2nd: 2 days  
- 3rd: 4 days  
- 4th: 7 days  
- 5th: 15 days  
- 6th and later: 30 days  

### Filtering

These are ignored:

- **System**: `.DS_Store`, `Thumbs.db`, `desktop.ini`
- **Temporary**: `.tmp`, `.temp`, `.part`, `.crdownload`
- **Dirs**: `.git`, `node_modules`, `venv`, `__pycache__`, `.cache`

### Data

- Stored in `~/.learning-review/library.json`
- Includes file paths, add time, last review time, and interval
- No cloud; everything stays on your Mac

## 💡 Tips

1. **Batch add**: Put related materials in one folder and add the folder
2. **Check settings**: Review reminder time and folders every so often
3. **Start small**: Add a manageable number of files at first
4. **Stay consistent**: Follow the suggested review schedule for best results

## 🔧 Troubleshooting

### No notifications
- Allow notifications for the app in **System Settings → Notifications**
- In the app, ensure schedule is **On** (Settings tab)

### App won’t start
- Use Python 3.7 or newer
- Install PyObjC: `pip install pyobjc`
- Check `/tmp/learning_review_error.log` for errors

### Files missing from today’s list
- They may match the ignore rules above
- Confirm paths are valid
- Try adding the file or folder again

## 📝 Info

- **Version**: 1.0  
- **Platform**: macOS  
- **Language**: Python 3  
- **GUI**: PyObjC (native macOS)

## 🤝 Feedback

If you run into issues or have ideas, open an issue or discussion on GitHub.

---

**FileRecall** — review smarter, remember longer. 🎓✨
