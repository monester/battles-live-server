Description
-----------

Application to show World Of Tanks battles.

URL: https://battles.universe.cc

Deploy
------

```bash
helm package chart/ --app-version latest
helm upgrade --install test battles-live-server-0.1.0.tgz --set wargaming_key=xxxx
```
