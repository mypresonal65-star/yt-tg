# Telegram Bot & Channel Setup Guide

Follow these 5 simple steps to get all required credentials for your `.env` file.

---

### Step 1: Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`

1. Open your browser and go to: **[https://my.telegram.org](https://my.telegram.org)**
2. Enter your phone number (with country code, e.g. `+1...` or `+91...`).
3. Telegram will send a confirmation code to your Telegram app. Enter it to log in.
4. Click on **API development tools**.
5. Fill in the short form:
   * **App title**: `ytdl-uploader` (or any name)
   * **Short name**: `ytdluploader`
   * **Platform**: Desktop (or Other)
6. Click **Create application**.
7. You will now see your **`App api_id`** (numbers) and **`App api_hash`** (letters and numbers).
   * Copy them into your `.env` file:
     ```env
     TELEGRAM_API_ID=12345678
     TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789
     ```

---

### Step 2: Create Your Bot with `@BotFather`

1. In the Telegram app, search for **`@BotFather`** (make sure it has the blue verified checkmark).
2. Click **Start** or send the message `/newbot`.
3. Choose a friendly display name for your bot (e.g. `My YouTube Downloader Bot`).
4. Choose a unique username ending in `bot` (e.g. `my_yt_dl_tube_bot`).
5. BotFather will reply with your **HTTP API Token**:
   * Example: `7123456789:AAFlkjhsdf98723kjsdhf-asdjfh`
   * Copy this into your `.env` file:
     ```env
     TELEGRAM_BOT_TOKEN=7123456789:AAFlkjhsdf98723kjsdhf-asdjfh
     ```

---

### Step 3: Create a Private Channel and Add the Bot as Admin

1. In Telegram, open the menu (three lines or pencil icon) and click **New Channel**.
2. Give your channel a name (e.g. `YouTube Archive`) and click **Create**.
3. Choose **Private Channel** and save.
4. Open the Channel's settings / Info:
   * Go to **Administrators** $\rightarrow$ **Add Administrator**.
   * Search for your bot's username (e.g. `@my_yt_dl_tube_bot`).
   * Add the bot and make sure the permission **"Post Messages"** is **enabled**.

---

### Step 4: Find Your Channel ID

Private channel IDs in Telegram always start with `-100`. Here is the easiest way to find yours:

**Method A (Easiest — via Forwarding Bot)**:
1. In your new channel, send any test message (e.g., "Hello").
2. Forward that test message to the bot **`@JsonDumpBot`** or **`@userinfobot`**.
3. The bot will reply with message details: look for `forward_from_chat` $\rightarrow$ `id`.
4. It will look like: `-1002345678901`.

**Method B (Telegram Web)**:
1. Open [https://web.telegram.org](https://web.telegram.org) in your browser.
2. Click on your channel.
3. Look at the URL in your browser address bar:
   * E.g.: `https://web.telegram.org/a/#-1002345678901`
   * The number starting with `-100...` is your `TELEGRAM_CHANNEL_ID`.

Copy it into your `.env`:
```env
TELEGRAM_CHANNEL_ID=-1002345678901
```

---

### Step 5: Test the Uploader

1. Install the updated requirements:
   ```powershell
   pip install -r requirements.txt
   ```
2. Run the test command:
   ```powershell
   python uploader.py
   ```
   Paste the path to any sample `.mp4` file to watch it upload to your channel with a live progress bar!
