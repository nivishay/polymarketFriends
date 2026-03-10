# FriendMarket

FriendMarket is a small Flask app for private yes/no prediction markets among friends. It uses account login, shareable invite links, and an internal balance ledger.

## Features

- Create a user account and log in
- Create a yes/no market with a fixed buy-in
- Share a market invite link with friends
- Join a market on YES or NO
- Resolve a market as the creator and distribute the pot internally

## Local run

```powershell
C:/Users/nivii/OneDrive/Desktop/test/.venv/Scripts/python.exe -m pip install -r requirements.txt
C:/Users/nivii/OneDrive/Desktop/test/.venv/Scripts/python.exe app.py
```

Open `http://127.0.0.1:5000`.

## Deploy on Render

1. Push this folder to a GitHub repository.
2. Create a new Render Web Service from that repository.
3. Render will detect `render.yaml` and use the included build and start commands.
4. After deploy, open the generated URL on your phone and share invite links from the app.

## Notes

- This is a demo ledger, not a real-money or wallet-backed system.
- SQLite is fine for a prototype. For multi-user production use, move to Postgres.