# Root conftest so pytest puts the repo root on sys.path (prepend import mode),
# letting tests import the top-level modules (faultmaven, store, rendering,
# listeners, config) without an installed package.
