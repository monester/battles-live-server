# build container
FROM python:3.8 AS build

RUN pip install pipenv; python -m venv /venv

ENV VIRTUAL_ENV /venv
ENV PATH=/venv/bin:$PATH

ADD Pipfile* /
RUN pipenv install

# running container
FROM python:3.8-slim

ENV VIRTUAL_ENV /venv
ENV PATH=/venv/bin:$PATH
ENV PS1="(venv) $PS1"

COPY --from=build /venv /venv
RUN mkdir /app
WORKDIR /app
ADD *.py /app/

CMD /app/server.py
