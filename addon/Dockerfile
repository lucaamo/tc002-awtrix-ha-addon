ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip3 install --no-cache-dir .
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
