# Fuji Silvertech – Phishing Awareness

## Run
1. Python 3.10+; `pip install -r requirements.txt`.
2. Export the variables in `.env.example` in your environment. Configure an authorized SMTP sender and a long random SECRET_KEY.
3. Run `flask --app app run --host 127.0.0.1 --port 5000`.
4. Visit http://127.0.0.1:5000.

Participants receive a one-time email code and must use the exact `@fujisilvertech.com` domain. Admin addresses are separately listed in `ADMIN_EMAILS`; the initial target is Akshay.Korukonda@smartims.com and is not active until that mailbox is verified. Add team admins individually.

## Production warning
This is a runnable starter, not a production-certified deployment. Before use by 300 employees, deploy behind HTTPS, add CSRF protection, rate limiting, MFA for admins, secure cookie settings, PostgreSQL and backups, retention controls and a security review. SQLite is suitable only for a single-instance test. Do not expose the database file. Company SSO requires identity-provider details and configuration.

Includes the supplied Fuji logo, A/B reference and ten supplied MP4s. The question videos are mapped by filename order pending review of each clip's content.
