To publish:

Copy .env.example → .env and set:


DATABASE_URL=postgresql://user:pass@host:5432/dbname
On Zappa, add to zappa_settings.json:


"environment_variables": { "DATABASE_URL": "postgresql://..." }