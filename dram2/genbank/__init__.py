import click

__version__ = '2.0.0'

@click.command('generate_genbank')
@click.version_option(__version__)
def generate_genbank():
    print("This comand is comming soon")
