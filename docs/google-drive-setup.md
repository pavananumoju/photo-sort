# Google Drive login (one time, free)

`photo-sort` talks to your Drive as *you*, through a personal OAuth client you
create once. Google does not charge for this.

## 1. Make an OAuth client

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library →** search "Google Drive API" → **Enable**.
3. **APIs & Services → OAuth consent screen:**
   - User type: **External**.
   - Fill the required name/email fields, skip the rest, save.
   - **Audience / Test users → Add users →** add your own Google address.
     (Leaving the app in "Testing" is fine for personal use — no Google review
     needed. Tokens for a testing app expire weekly, so you re-login about once a
     week.)
4. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   - Application type: **Desktop app**.
   - Create, then **Download JSON**.
5. Save that file in the project folder as **`client_secret.json`**.

## 2. First run

```bash
photo-sort scan
```

A browser window opens asking you to allow Drive access. Approve it. A
`token.json` is written and reused afterwards. Both `client_secret.json` and
`token.json` are git-ignored — do not commit or share them.

## Why the tool asks for full Drive access

It needs to **move** existing photos into a `photo-sort review` folder. The
narrower `drive.file` scope only lets an app see files it created itself, which
would make it blind to your existing photos. The tool never deletes; a
quarantined photo is one folder-move from where it was and stays recoverable in
Drive Trash for 30 days if you empty it yourself.

## Revoking access

<https://myaccount.google.com/permissions> → remove the project. Delete
`token.json` locally.
