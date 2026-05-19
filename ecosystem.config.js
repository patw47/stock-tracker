module.exports = {
  apps: [{
    name: 'stock-tracker',
    script: 'n8n',
    args: 'start',
    env: {
      N8N_PORT: process.env.N8N_PORT,
      N8N_USER_FOLDER: process.env.N8N_USER_FOLDER,
      N8N_BASIC_AUTH_ACTIVE: process.env.N8N_BASIC_AUTH_ACTIVE,
      N8N_BASIC_AUTH_USER: process.env.N8N_BASIC_AUTH_USER,
      N8N_BASIC_AUTH_PASSWORD: process.env.N8N_BASIC_AUTH_PASSWORD,
      GENERIC_TIMEZONE: process.env.GENERIC_TIMEZONE,
      N8N_DEFAULT_LOCALE: process.env.N8N_DEFAULT_LOCALE
    }
  }]
};