#!/bin/sh
# serve: run the scheduler, and keep the container up as a home for `docker exec -it media-health bash`
if [ "$1" = "serve" ]; then
    exec media-health --serve
fi
exec media-health "$@"
