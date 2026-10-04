# Play Store Price Drop Tracker

This tracker checks the **base price** of Android apps on the Google Play Store (ignoring in-app purchases). Whenever a price goes down, it sends you a Telegram message.

It runs as a scheduled job on GitHub Actions. There is no server to keep running and no external database. The price history lives in a single SQLite file, `prices.db`, which the workflow commits back to your repository after each run.

## Project layout

```
track_prices.py        Main script
apps.json              List of apps to track
prices.db              SQLite database (created automatically, committed by GitHub Actions)
requirements.txt       Python dependencies
.gitignore             Keeps secrets and junk out of Git
.github/workflows/daily.yml   Scheduled GitHub Actions workflow
```

## 1. Installation

You need Python 3.9 or newer.

```bash
python -m venv venv
```

Activate the environment:

- macOS / Linux: `source venv/bin/activate`
- Windows (PowerShell): `venv\Scripts\Activate.ps1`

Then install the dependencies:

```bash
pip install -r requirements.txt
```

## 2. Configuration: `apps.json`

Edit `apps.json` to choose which apps to track:

```json
[
    {
        "package_id": "com.spotify.music",
        "country": "in"
    }
]
```

- `package_id` is the part after `id=` in a Play Store URL. For example, `https://play.google.com/store/apps/details?id=com.spotify.music` gives `com.spotify.music`.
- `country` is a two-letter code such as `in`, `us` or `gb`. This controls which regional price is checked.

To add an app, add another object inside the list, separated by a comma. To remove one, delete its object. You don't need to change any Python code.

## 3. Telegram setup

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot`, then follow the prompts. BotFather will give you a **bot token**. Keep it private.
3. Open a chat with your new bot and send it any message (for example, "hi").
4. Find your **chat ID**. One easy way: open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and look for `"chat":{"id": ... }`. The number there is your chat ID.

Never paste your real token into the README, the code, or any file you commit.

## 4. Local testing

Create a file named `.env` in the project folder (it is already ignored by Git):

```text
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

Run the tracker:

```bash
python track_prices.py
```

The first run stores baseline prices in `prices.db` and sends no notifications. Run it again later, and if a price has dropped, you'll get a Telegram message.

To test the notification without waiting for a real drop, you can temporarily edit the stored price in `prices.db` to a higher value using any SQLite browser, then run the script again.

## 5. Git initialization

From the project folder:

```bash
git init
git add .
git commit -m "Initial commit"
```

Confirm that `.env` is **not** listed by `git status`.

## 6. GitHub repository setup

1. On GitHub, click **New repository**. Name it, for example, `play-store-price-tracker`. You can leave it private.
2. Push your local project:

   ```bash
   git branch -M main
   git remote add origin https://github.com/<your-username>/play-store-price-tracker.git
   git push -u origin main
   ```

3. In the repository, open **Settings → Secrets and variables → Actions**.
4. Click **New repository secret** and add both:
   - `TELEGRAM_BOT_TOKEN`: the token from BotFather
   - `TELEGRAM_CHAT_ID`: your chat ID
5. Go to the **Actions** tab. If GitHub asks you to enable workflows, click the button to enable them.

### Manual testing

1. Open the **Actions** tab and select **Play Store Price Tracker**.
2. Click **Run workflow**, then **Run workflow** again to confirm.
3. Open the run and check the logs. You should see the baseline message for each app on the first run.
4. Confirm the workflow succeeded (green check).

## 7. Automation and schedule

The workflow runs automatically at **04:00 and 16:00 UTC** every day. GitHub Actions cron uses UTC, so convert to your local time:

| UTC    | India (IST, UTC+5:30) |
|--------|-----------------------|
| 04:00  | 09:30                 |
| 16:00  | 21:30                 |

To change the times, edit the `cron` line in `.github/workflows/daily.yml`. Use the [cron syntax](https://crontab.guru/) and remember it is UTC.

**Verifying the schedule:** after the first scheduled run, check the **Actions** tab to see a run triggered by "schedule". GitHub may delay scheduled runs by a few minutes during busy periods, and scheduled workflows in repositories with no activity for about 60 days are paused automatically. If that happens, re-enable the workflow from the Actions tab.

## 8. Database

`prices.db` is an **SQLite** database stored as a regular file in the repository. It holds one row per app and country, with:

- `current_price`: the most recently fetched price
- `previous_price`: the price before the most recent change
- `last_checked` and `last_changed`: timestamps

Each scheduled run reads this file, compares prices, updates it, and then commits the change to GitHub. The next run therefore sees the previous run's data. The workflow only commits when the file actually changed.

Because the database is in Git, you can see the full history of changes in the repository's commit log.

## Troubleshooting

- **"Telegram credentials missing"**: check that `.env` exists locally, or that both secrets exist in GitHub.
- **"Play Store error"**: the package ID or country may be wrong, or the app may be unavailable in that country.
- **No commit appears after a run**: the price didn't change, so there was nothing new to save.
- **Push fails with a permission error**: check that the workflow's `permissions: contents: write` block is present, and that branch protection isn't blocking the bot.
