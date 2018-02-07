import ConfigParser

DEFAULT_SETTINGS = {
    'nicehash': {
        'wallet': '',
        'region': 'usa'
    },
    'excavator': {
        'enabled': True,
        'path': '/opt/excavator/bin/excavator',
        'port': 3456
        }
    }

def read_settings_from_file(fd):
    settings = {}
    parser = ConfigParser.SafeConfigParser()
    parser.readfp(fd)

    def get_option(parser_method, section, option):
        try:
            return parser_method(section, option)
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            return DEFAULT_SETTINGS[section][option]

    nicehash = {}
    nicehash['wallet'] = get_option(parser.get, 'nicehash', 'wallet')
    nicehash['region'] = get_option(parser.get, 'nicehash', 'region')
    settings['nicehash'] = nicehash

    excavator = {}
    excavator['enabled'] = get_option(parser.getboolean, 'excavator', 'enabled')
    excavator['path'] = get_option(parser.get, 'excavator', 'path')
    excavator['port'] = get_option(parser.getint, 'excavator', 'port')
    settings['excavator'] = excavator

    return settings

def write_settings_to_file(fd, settings):
    parser = ConfigParser.SafeConfigParser()

    for section in settings:
        parser.add_section(section)
        for option in settings[section]:
            value = settings[section][option]
            parser.set(section, option, str(value))

    parser.write(fd)
