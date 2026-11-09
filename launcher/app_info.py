"""Built-in facts about this app. PUBLIC_KEY_HEX checks update signatures (it's public on
purpose); the matching private key lives only in the repo's UPDATE_SIGNING_KEY secret."""
APP_NAME = "Fisch Macro"
REPO = "VinnyInnit/fisch-macro"        # where releases (and updates) are published
SHELL_VERSION = 1                      # bump when the app itself (not just the macro) changes
PUBLIC_KEY_HEX = "1cc89f76bab2936bf3e40d132a59065fae19d9071c1a18d1bb531c3dc2806f9d"
