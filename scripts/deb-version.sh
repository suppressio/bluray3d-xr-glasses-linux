#!/usr/bin/env bash

# Debian package version from the app version (git describe):
#   v1.2.0              -> 1.2.0
#   v1.2.0-rc.1         -> 1.2.0~rc.1           pre-release, sorts before 1.2.0
#   v1.2.0-3-gabc1234   -> 1.2.0+3.gabc1234     dev build, after 1.2.0, before 1.2.1
#   20811e4             -> 0~20811e4            no tag at all
# No hyphen is left: Debian would read what follows it as a Debian revision.
deb_package_version() {
    local package_version
    case "$1" in
        v[0-9]*)
            package_version=${1#v}
            # git describe's "-<commits>-g<hash>" tail of a dev build
            if [[ $package_version =~ ^(.*)-([0-9]+)-(g[0-9a-f]+)$ ]]; then
                package_version="${BASH_REMATCH[1]}+${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
            fi
            # what is left after a hyphen is a pre-release tag (rc.1, beta2)
            package_version=${package_version//-/\~}
            ;;
        *) package_version="0~$1" ;;
    esac
    printf '%s\n' "$package_version"
}
