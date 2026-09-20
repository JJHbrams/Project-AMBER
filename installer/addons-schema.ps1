#
# addons-schema.ps1 -- installer/addons/addons.json manifest schema version single source of truth.
# build-addons.ps1 (writer) and build-addons-includes.ps1 (reader) must not each
# hold their own literal schema number -- if only one changes, they silently diverge.
# Always dot-source this file and use $EngramAddonManifestSchema instead of a literal.
#
$EngramAddonManifestSchema = 3
