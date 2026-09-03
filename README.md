# Join-request welcome + broadcast bot

One long-running Telethon bot. It watches the channels you add, sends every new
join requester your saved post, keeps them as an audience, and pushes any post to
that whole audience on `/bcast`.

## Why it can message strangers

Telegram lets a bot write to a user it has never talked to in exactly one case:
that user has a **pending join request** in a channel where the bot is an admin.
The bot receives that request as a live update, which is the moment it can reach
the person — and the moment it records them for later broadcasts.

## Quick start

```bash
git clone https://github.com/TrishaAdio/Req.git && cd Req
python3 setup.py
```

`setup.py` builds `.venv`, installs the requirements, creates `.env` from the
example and asks for the four values it needs, then starts the bot. Later runs
skip straight to starting it — the install is only redone when
`requirements.txt` changes.

```bash
python3 setup.py --install     # set up, don't start
python3 setup.py --update      # reinstall requirements, then start
python3 setup.py --recreate    # rebuild .venv from scratch
```

Prefer doing it by hand:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # API_ID, API_HASH, BOT_TOKEN, OWNER_ID
.venv/bin/python bot.py
```

Python 3.9 or newer.

In every channel you want served:

1. Make the bot an **admin** with the **Add users** right.
2. Turn on **Approve new members** — that is what creates join requests.
3. Send the bot `/add -1001234567890` (a bare id, `@username` or a `t.me/...`
   link work too). Nothing is served until you do — set `STRICT_CHANNELS=false`
   if you would rather have every chat the bot administers served by default.

Then send the bot the message you want delivered and reply to it with
`/setpost`. `/preview` shows exactly what a joiner receives.

Don't know your own user id? Start the bot with `OWNER_ID` empty, message it,
and the log prints the id to use.

## Commands

Owner only. They work anywhere you write them — the bot's DM, a group, a topic.

| Command | Effect |
|---|---|
| `/add <chat_id> [...]` | Serve those channels' join requests (additive) |
| `/remove <chat_id>` | Stop serving that channel |
| `/chats` | Served channels with how many users each brought in |
| `/setpost` | **Reply** to a message to make it the post |
| `/clearpost` | Drop the saved post |
| `/setbutton` | Inline URL buttons for the post |
| `/clearbutton` | Drop the buttons |
| `/preview` | Send the post to yourself |
| `/bcast` | Send the post (or a replied post) to every user |
| `/cancel` | Stop a running broadcast |
| `/stats` | Users, new today, reachable, channels, post |
| `/export` | `users.json`, always sent to your own chat with the bot |

`/bcast` edits one live progress message with sent/failed counts, rate and ETA.

### Buttons

```
/setbutton
Join - https://t.me/x | Chat - https://t.me/y
Site - https://example.com
```

One row per line, `|` splits buttons inside a row.

## What survives the copy

The post is re-sent by reference, never re-uploaded, and the original message
entities are passed through untouched. So text, media, spoilers, links, bold,
and **premium (custom) emoji** all arrive as you wrote them. Recipients see a
normal message, not a forward. A caption longer than the 1024-unit bot limit is
split: media first, full text right after, so nothing is silently cut.

Albums are the exception — one message id is stored, so `/setpost` rejects a
grouped post instead of delivering only its first item.

## Pacing

Sending is paced at `BROADCAST_WORKERS / SEND_DELAY_SECONDS` messages per second
— ~1/s by default — with a pause every `BATCH_SIZE` sends. These are DMs to
people who never started the bot, the traffic Telegram limits hardest, so the
defaults are slow on purpose; raising them raises your odds of `PeerFlood`.

One flood gate is shared by welcomes and broadcasts, so a limit found by either
slows both down instead of each rediscovering it. A hard `PeerFlood`, or a flood
wait longer than `MAX_FLOOD_WAIT`, stops the run rather than grinding through the
rest of the queue and failing every send. Users who block the bot, delete their
account, or turn out to be bots are marked and skipped by later runs.

A broadcast lives in memory: it does not resume after a restart, and starting it
again sends to everyone, including those already reached. `/cancel` before
restarting.

## Who gets welcomed

Every join request gets the post — including requests from people the bot has
already welcomed, so someone who leaves and asks to join again, or who requests
a second served channel, is welcomed each time.

Set `WELCOME_ONCE=true` for the other behaviour: one post per user for as long
as they are in `users.json`, with every later request from them sending nothing.
That also collapses the two requests you get when someone joins two channels at
once into a single message.

Either way the request is always recorded, so the user is in the audience for
`/bcast` even when no post is delivered.

## The daily cap

`DAILY_USER_LIMIT=2` is the ceiling on what one person receives from the bot in
any 24 hours. Welcomes and broadcasts draw on the same allowance, so a requester
can be welcomed twice, or welcomed once and then reached by one broadcast — the
third message is not sent. `0` removes the cap.

The window is rolling, not a calendar day: two messages at 23:59 do not free the
allowance up again a minute later. Stamps age out individually, so the allowance
returns gradually rather than all at once.

`OWNER_ID` and `ADMIN_IDS` are exempt, so `/preview` and testing a broadcast on
yourself work however often you run them.

A user at the cap is still recorded and still counted in `/stats`, which reports
the cap and how many users are sitting at it. A broadcast reports them as
`at daily cap` instead of quietly sending or quietly dropping them. Nothing is
sent to them until their window opens; the message is skipped, not queued.

## Layout

| Path | Role |
|---|---|
| `setup.py` | Builds `.venv`, installs requirements, checks `.env`, starts the bot |
| `bot.py` | Entry point: startup, banner, graceful shutdown |
| `app/config.py` | Environment configuration |
| `app/log.py` | Colourised logging (colorama) |
| `app/storage.py` | Atomic JSON stores with coalesced writes |
| `app/users.py` | Audience + per-user status (`data/users.json`) |
| `app/channels.py` | Served channels (`data/channels.json`) |
| `app/post.py` | The saved post: reference, cache, resolving |
| `app/buttons.py` | Inline URL buttons (`data/buttons.json`) |
| `app/copier.py` | Copy one message to one user |
| `app/broadcast.py` | Worker pool, flood back-off, progress, cancel |
| `app/handlers.py` | Join requests + owner commands |
| `deploy/req.service` | systemd unit |

An older flat `data/audience.json` (from the previous version of this bot) is
imported automatically on first start.

## Deploy

```bash
sudo git clone https://github.com/TrishaAdio/Req.git /opt/req
cd /opt/req && sudo python3 setup.py --install
sudo cp deploy/req.service /etc/systemd/system/
sudo systemctl enable --now req
journalctl -u req -f
```

## Notes

- Join requests made **before** the bot was added can't be listed by a bot —
  only live ones are caught.
- Never commit `.env` or `*.session`; `.gitignore` already covers them.
- Colours turn themselves off when output isn't a terminal (`NO_COLOR=1` /
  `FORCE_COLOR=1` override).
