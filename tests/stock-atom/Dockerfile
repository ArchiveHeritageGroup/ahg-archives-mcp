# Stock AtoM (no AHG plugins) for testing the MCP's atom-base mode: the
# official release tarball, PHP-FPM and nginx in one container.
FROM php:8.3-fpm-bookworm
ARG ATOM_VERSION=2.10.2
RUN apt-get update && apt-get install -y --no-install-recommends nginx curl libxslt1-dev libzip-dev libicu-dev \
      default-mysql-client \
 && docker-php-ext-install pdo_mysql xsl zip intl opcache \
 && pecl install apcu && docker-php-ext-enable apcu \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://storage.accesstomemory.org/releases/atom-${ATOM_VERSION}.tar.gz | tar -xz -C / \
 && mv /atom-${ATOM_VERSION} /atom && chown -R www-data:www-data /atom
COPY nginx.conf /etc/nginx/sites-enabled/default
COPY setup.sh fixtures.csv /opt/
RUN printf 'memory_limit=512M\nmax_execution_time=120\n' > /usr/local/etc/php/conf.d/atom.ini
WORKDIR /atom
CMD ["sh", "-c", "php-fpm -D && nginx -g 'daemon off;'"]
