# Privacy Policy for Panem Sportsbook

**Effective Date:** September 23, 2026 
**Last Updated:** September 23, 2026

Welcome to **Panem Sportsbook** ("we", "us", "our", or the "Application"). Panem Sportsbook is an interactive, fan-made, community entertainment simulation designed for the Interatctive Discord Hunger Games server running Hunger Games-themed roleplay. The Application encompasses:

1. The **Panem Sportsbook Discord Bot** (slash commands such as `/bet`, `/parlay`, `/odds`, `/balance`, `/mybets`, etc.),
2. The **Panem Sportsbook Discord Activity** (an embedded application accessible inside Discord voice and text channels), and
3. The **Panem Sportsbook Web Portal** (hosted at `https://panem-sportsbook.phqen1x.com/` or designated community domains).

This Privacy Policy explains how we collect, use, store, disclose, and protect information when you interact with our Discord bot, embedded Discord Activity, and web application.

---

## 1. Important Disclaimers

> [!NOTE]
> **Non-Commercial Fan Project:** Panem Sportsbook is an unofficial, non-commercial, fan-created project inspired by *The Hunger Games* franchise. It is not affiliated with, endorsed by, or sponsored by Suzanne Collins, Lionsgate Entertainment, Scholastic, or any of their affiliates.

> [!IMPORTANT]
> **No Real-Money Gambling:** Panem Sportsbook is strictly a virtual simulation and roleplay game. The currencies used within the Application ("Chips" and server "Panars") are fictional digital tokens with **no real-world monetary value**. We do not collect payment card information, process financial transactions, or offer real-money payouts.

---

## 2. Information We Collect

We only collect information necessary to provide, operate, secure, and maintain the functionality of the Application.

### A. Information Provided via Discord
When you interact with the Discord bot, authorize the Discord Activity, or log into the web portal via Discord OAuth2, we receive certain account information directly from Discord pursuant to your Discord privacy settings:
- **Discord User ID (Snowflake):** A unique numerical identifier assigned by Discord, used to identify your account, track balances, and associate your bets.
- **Discord Username and Global Display Name:** Used to identify you on leaderboards, bet slips, public parlay listings, and user interfaces.
- **Discord Avatar Hash / URL:** Used to display your profile image within the web dashboard and Discord Activity.
- **Guild / Server Membership & Roles:** Used to verify that you are a member of an authorized server and whether you hold administrator or Gamemaker permissions (e.g., `ADMIN_ROLE_ID`).

### B. Gameplay and In-App Data
As you participate in the sportsbook, we automatically create and maintain game-related records:
- **Virtual Currency Balances:** Your current virtual chip balance, bonus bets, profit boost tokens, deposit match claims, and shop purchases.
- **Wagering Activity:** Records of straight bets and parlays placed, leg selections, odds locked at time of wager, timestamps, amounts wagered, payouts, won/loss statuses, and cashout events.
- **Public Bets & Custom Content:** Custom titles or names you assign to parlays, as well as parlays you publish to the community "Tail Board."
- **Virtual Exchange Requests:** Records of `/deposit` or `/withdraw` requests submitted to convert server roleplay currency (Panars) to or from virtual chips.
- **Leaderboard & Gameplay Statistics:** Aggregated performance statistics such as total chips wagered, total won, and win/loss counts.

### C. Technical and Web Data
When you visit the web dashboard or launch the embedded Discord Activity:
- **Session and Authentication Tokens:** Cryptographically signed session cookies (`thg_session`), anti-forgery CSRF tokens, and short-lived signed bearer tokens for the Discord Activity.
- **Server Logs:** Standard web server request logs, including your IP address, browser user-agent, operating system, requested endpoints, HTTP status codes, and timestamps. These are collected strictly for debugging, performance monitoring, security diagnostics, and rate-limiting.
- **Diagnostic and Error Logs:** Error reports generated when commands fail or exceptions occur.

### D. Information We NEVER Collect
- Real-world financial or banking information (e.g., credit/debit card numbers, bank accounts, PayPal/crypto wallet addresses).
- Real names, physical mailing addresses, or phone numbers.
- Passwords (all authentication is handled securely via Discord OAuth2 / Discord SDK; we never see or store your Discord password).
- Private Discord messages, voice chat recordings, or direct messages outside of bot interactions.

---

## 3. How We Use Your Information

We use the collected data solely for the following operational purposes:
1. **Providing Core Gameplay:** Tracking chip balances, verifying bet eligibility, calculating dynamic odds, settling open markets, and generating leaderboard rankings.
2. **Authentication & Authorization:** Verifying your identity across the web panel and Discord Activity, and confirming administrative rights for Gamemakers managing markets and tributes.
3. **Displaying Community Features:** Rendering bet slips, displaying public parlays on the community tail board, and showing leaderboard standings.
4. **Preventing Abuse & Exploitation:** Detecting botting, rapid duplicate requests, rate-limit violations, balance manipulation, or unauthorized administrative actions.
5. **Technical Troubleshooting:** Analyzing error logs to fix software bugs and improve application reliability.

We do **not** sell, rent, monetize, or trade your personal data to any third parties or advertisers.

---

## 4. Third-Party Services and Data Sharing

We do not disclose your personal information except to the third-party platforms required to operate the Application:

- **Discord Inc.:** The bot and Activity operate on the Discord platform. Use of our service is subject to [Discord’s Privacy Policy](https://discord.com/privacy) and [Discord's Developer Terms](https://discord.com/developers/docs/policies-and-agreements/developer-terms-of-service).
- **Hosting & Infrastructure Providers:** Our web server, SQLite databases, and reverse proxy/tunnel services (e.g., Cloudflare, VPS provider) process requests to deliver network traffic securely.
- **Local AI Commentary (Lemonade SDK):** When automated flavor text or odds commentary is generated using our integrated local AI engine, data is processed locally on our host server. No personal data is sent to external, commercial third-party LLM providers.
- **Legal Compliance:** We may disclose minimal records if required by valid legal process, law enforcement subpoena, or to protect the safety, security, and rights of our users and administrators.

---

## 5. Data Storage, Retention, and Security

### A. Data Storage
All persistent data (user profiles, balances, bets, markets, and server settings) is stored in localized SQLite database files (`sportsbook_<guild_id>.db`) segregated per Discord server.

### B. Security Measures
We implement standard technical and organizational security controls:
- **Server-Side Secret Keys:** Web session cookies and Discord Activity bearer tokens are cryptographically signed using a secure secret key (`WEB_SECRET_KEY`).
- **CSRF & Header Protection:** Form endpoints are guarded by Cross-Site Request Forgery (CSRF) tokens. Web responses enforce Content Security Policies (CSP), `X-Frame-Options`, and `X-Content-Type-Options: nosniff`.
- **Discord Verification:** Admin privileges are verified against authoritative Discord API role data on the server side—never trusted from client claims.

### C. Data Retention
- **Active Season Data:** Player profiles, balances, and betting logs are maintained for the duration of the active Hunger Games season or tournament.
- **Season Resets / Archives:** Server administrators may archive or reset databases at the conclusion of a season.
- **Server Logs:** Web and debug logs are periodically rotated and purged.

---

## 6. Your Rights and Data Controls

Depending on your jurisdiction (such as under GDPR or CCPA) and Discord’s platform guidelines, you have rights regarding your information:

- **Access and Portability:** You can view your current data at any time via `/balance`, `/mybets`, `/leaderboard`, or by logging into the web dashboard or Discord Activity.
- **Correction:** If your username or avatar changes on Discord, logging in or interacting with the bot will refresh your stored profile data.
- **Deletion (Right to be Forgotten):** You may request the deletion of your user account record, betting history, and chip balance. To request data deletion, contact a server administrator or reach out using the contact information below. Upon confirmation, your records will be purged or irreversibly anonymized in the database.
- **Opting Out of Public Features:** When submitting parlays, you can mark them private to exclude them from the public community tail board, or remove existing public slips using `/parlay unlist`.

---

## 7. Children's Privacy

Panem Sportsbook is not directed to children under the age of 13 (or under the minimum digital consent age in your country, e.g., 16 in certain European jurisdictions). In accordance with Discord's Terms of Service, all users must meet Discord's minimum age requirements. We do not knowingly collect personal data from anyone under the required age. If we discover that a user is under the minimum age, we will promptly delete their stored data.

---

## 8. Changes to This Privacy Policy

We may update this Privacy Policy from time to time to reflect changes in our gameplay mechanics, bot features, or legal obligations. When updates occur:
- We will update the **Last Updated** date at the top of this document.
- Significant changes may be announced via the Discord server's announcement channel (`/admin announce`).

Your continued use of Panem Sportsbook after any revisions indicates your acceptance of the updated Privacy Policy.

---

## 9. Contact Us

If you have questions, concerns, or requests regarding this Privacy Policy or your data, please contact us:

- **Discord Server:** [Interactive Discord Hunger Games](https://discord.gg/sunriseonthereaping)
- **Discord Contact / Bot Administrator:** @phqen1x
- **Email:** rustyphqenix@gmail.com
- **GitHub Repository:** [https://github.com/phqen1x/thg-sportsbook](https://github.com/phqen1x/thg-sportsbook)
