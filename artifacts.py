"""Known rebuildable project directories. Never match generic names alone."""
import json
import re

TYPES = ('node_modules', 'rust', 'gradle', 'flutter', 'ios_build', 'cocoapods', 'web', 'dotnet', 'swift', 'cmake', 'pnpm', 'terraform')
NAMES = {'node_modules', 'target', 'build', '.gradle', '.cxx', '.dart_tool',
         'DerivedData', 'Pods', '.next', '.nuxt', '.output', '.turbo', '.expo',
         '.parcel-cache', 'cache', 'dist', 'bin', 'obj', '.build', '.pnpm-store', 'providers'}
MARKERS = {'package.json', 'Cargo.toml', 'pubspec.yaml', 'Podfile',
           'build.gradle', 'build.gradle.kts', 'settings.gradle', 'settings.gradle.kts',
           'Package.swift', 'CMakeLists.txt'}
DOTNET_SUFFIXES = ('.csproj', '.fsproj', '.vbproj')


def is_project(files, dirs):
    return bool(MARKERS.intersection(files)) or any(n.endswith(('.tf', '.tf.json')) for n in files) or any(
        n.endswith(DOTNET_SUFFIXES) for n in files
    ) or any(n.endswith('.xcodeproj') for n in dirs)


def classify(parent, name, files):
    """Return a catalog type based on the directory name and adjacent manifests."""
    if name not in NAMES:
        return None
    files = set(files)
    target = parent / name
    gradle = bool(files.intersection({'build.gradle', 'build.gradle.kts',
                                     'settings.gradle', 'settings.gradle.kts'}))
    if name == 'node_modules':
        return 'node_modules'
    if name == '.pnpm-store' and 'package.json' in files and (parent / 'pnpm-lock.yaml').is_file():
        return 'pnpm'
    if name == 'providers' and parent.name == '.terraform' and any(
        p.is_file() and not p.is_symlink() and p.name.endswith(('.tf', '.tf.json'))
        for p in parent.parent.iterdir()
    ):
        return 'terraform'
    if name == 'target' and 'Cargo.toml' in files:
        return 'rust'
    if name == '.build' and 'Package.swift' in files:
        return 'swift'
    if name == 'build' and 'CMakeLists.txt' in files and (target / 'CMakeCache.txt').is_file():
        return 'cmake'
    if name in {'build', '.gradle', '.cxx'} and gradle:
        return 'gradle'
    if name in {'build', '.dart_tool'} and 'pubspec.yaml' in files:
        if name == '.dart_tool' or 'sdk: flutter' in (parent / 'pubspec.yaml').read_text():
            return 'flutter'
    if name == 'Pods' and {'Podfile', 'Podfile.lock'} <= files:
        return 'cocoapods'
    if name in {'build', 'DerivedData'}:
        xcode = 'Podfile' in files or any(p.name.endswith('.xcodeproj') and p.is_dir()
                                       for p in parent.iterdir())
        if xcode:
            return 'ios_build'
    if name in {'bin', 'obj'} and any(n.endswith(DOTNET_SUFFIXES) for n in files):
        return 'dotnet'
    if name == 'cache' and parent.name == '.angular' and (parent.parent / 'angular.json').is_file():
        return 'web'
    if name in {'.next', '.nuxt', '.output', '.turbo', '.expo', '.parcel-cache', 'dist'} and 'package.json' in files:
        manifest = json.loads((parent / 'package.json').read_text())
        if not isinstance(manifest, dict):
            return None
        dependencies = set()
        for key in ('dependencies', 'devDependencies', 'peerDependencies'):
            if isinstance(manifest.get(key), dict):
                dependencies.update(manifest[key])
        if name == '.next' and ('next' in dependencies or any(parent.glob('next.config.*'))):
            return 'web'
        if name in {'.nuxt', '.output'} and ('nuxt' in dependencies or any(parent.glob('nuxt.config.*'))):
            return 'web'
        if name == '.turbo' and ('turbo' in dependencies or (parent / 'turbo.json').is_file()):
            return 'web'
        if name == '.expo' and 'expo' in dependencies:
            return 'web'
        if name == '.parcel-cache' and dependencies.intersection({'parcel', '@parcel/core'}):
            return 'web'
        if name == 'dist':
            scripts = manifest.get('scripts', {})
            if isinstance(scripts, dict) and any(
                'build' in key and isinstance(command, str) and re.search(
                    r'\b(tsc|nx|vite|next|nuxt|webpack|rollup|esbuild|tsup|nest|electron-builder|'
                    r'tauri|svelte-kit|vue-cli-service|astro|parcel|turbo|react-scripts)\b', command)
                for key, command in scripts.items()
            ):
                # The caller also requires this generic output directory to be Git-ignored.
                return 'web'
    return None
