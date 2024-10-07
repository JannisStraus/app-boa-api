try:
    from boa_ai._version import (
        __githash__,
        __version__
    )
except ImportError:
    print('Missing file "bca_ai/_version.py"')
    print('Run "./scripts/generate_version.sh" from the project root directory')
    __githash__ = 'N/A'
    __version__ = 'N/A'
