#!/bin/sh
# Installs stock AtoM into the throwaway containers, enables its REST API,
# loads fixtures.csv and prints a REST API key for the admin user.
# Run inside the atom container:  docker compose exec atom sh /opt/setup.sh
set -e
cd /atom
until mysqladmin ping -h db -uroot -patomtest --silent; do sleep 2; done
until curl -sf http://search:9200/_cluster/health >/dev/null; do sleep 2; done
su www-data -s /bin/sh -c "php -d memory_limit=-1 symfony tools:install --no-confirmation \
  --database-host=db --database-port=3306 --database-name=atom --database-user=root --database-password=atomtest \
  --search-host=search --search-port=9200 --search-index=atom \
  --site-title='Stock AtoM test' --site-description=test --site-base-url=http://atom.example.org \
  --admin-email=admin@example.org --admin-username=admin --admin-password=admin-test-only"
su www-data -s /bin/sh -c "php symfony tools:atom-plugins add arRestApiPlugin"
su www-data -s /bin/sh -c "php -d memory_limit=-1 symfony csv:import /opt/fixtures.csv --index"
# A reader with no groups: what a visitor may see, for the MCP's public service.
# add-superuser makes an administrator; taking that group away leaves a plain
# authenticated user.
su www-data -s /bin/sh -c "php symfony tools:add-superuser reader --email=reader@example.org --password=reader-test-only"
mysql -h db -uroot -patomtest atom -e "
  DELETE g FROM acl_user_group g JOIN user u ON u.id = g.user_id WHERE u.username = 'reader' AND g.group_id = 100;"
# REST API keys (stock AtoM keeps one as the user's restApiKey property).
for U in admin reader; do
  KEY=$(php -r 'echo bin2hex(random_bytes(16));')
  mysql -h db -uroot -patomtest atom -e "
    INSERT INTO property (object_id, name, source_culture) SELECT id, 'restApiKey', 'en' FROM user WHERE username = '$U';
    INSERT INTO property_i18n (id, culture, value) VALUES (LAST_INSERT_ID(), 'en', '$KEY');"
  echo "$(echo $U | tr a-z A-Z)_API_KEY=$KEY"
done
su www-data -s /bin/sh -c "php symfony cc" >/dev/null
