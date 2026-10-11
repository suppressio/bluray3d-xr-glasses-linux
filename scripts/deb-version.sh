#!/usr/bin/env bash

deb_package_version() {
    local package_version
    local suffix
    case "$1" in
        v[0-9]*)
            package_version=${1#v}
            case "$package_version" in
                *-*)
                    suffix=${package_version#*-}
                    case "$suffix" in
                        [!0-9]*) package_version=${package_version/-/\~} ;;
                    esac
                    ;;
            esac
            ;;
        *) package_version="0~$1" ;;
    esac
    printf '%s\n' "$package_version"
}
