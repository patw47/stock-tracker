module.exports = {
  apps: [{
    name: 'stock-tracker',
    script: 'n8n',
    args: 'start',
    env: {
      N8N_PORT: 5680,
      N8N_USER_FOLDER: '/home/acer/stock-tracker',
      N8N_BASIC_AUTH_ACTIVE: 'true',
      N8N_BASIC_AUTH_USER: 'admin',
      N8N_BASIC_AUTH_PASSWORD: 'stockwatcher2026',
      GENERIC_TIMEZONE: 'Europe/Paris',
      N8N_DEFAULT_LOCALE: 'fr'
    }
  }]
};
