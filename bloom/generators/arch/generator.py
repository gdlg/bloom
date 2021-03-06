# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import print_function

import collections
import datetime
import json
import os
import pkg_resources
import re
import shutil
import sys
import traceback
import textwrap

from tempfile import mkdtemp
from dateutil import tz
from distutils.version import LooseVersion
from time import strptime

from bloom.generators import BloomGenerator
from bloom.generators import GeneratorError
from bloom.generators import resolve_dependencies
from bloom.generators import update_rosdep

from bloom.generators.common import default_fallback_resolver
from bloom.generators.common import invalidate_view_cache
from bloom.generators.common import resolve_rosdep_key

from bloom.git import inbranch
from bloom.git import get_branches
from bloom.git import get_commit_hash
from bloom.git import get_current_branch
from bloom.git import has_changes
from bloom.git import show
from bloom.git import tag_exists

from bloom.logging import ansi
from bloom.logging import debug
from bloom.logging import enable_drop_first_log_prefix
from bloom.logging import error
from bloom.logging import fmt
from bloom.logging import info
from bloom.logging import warning

from bloom.commands.git.patch.common import get_patch_config
from bloom.commands.git.patch.common import set_patch_config

from bloom.packages import get_package_data

from bloom.util import code
from bloom.util import execute_command
from bloom.util import maybe_continue

from bloom.config import get_tracks_dict_raw

try:
    import rosdistro
except ImportError as err:
    debug(traceback.format_exc())
    error("rosdistro was not detected, please install it.", exit=True)

try:
    import em
except ImportError:
    debug(traceback.format_exc())
    error("empy was not detected, please install it.", exit=True)

# Drop the first log prefix for this command
enable_drop_first_log_prefix(True)

TEMPLATE_EXTENSION = '.em'


def __place_template_folder(group, src, dst):
    template_files = pkg_resources.resource_listdir(group, src)
    # For each template, place
    for template_file in template_files:
        template_path = os.path.join(src, template_file)
        template_dst = os.path.join(dst, template_file)
        if pkg_resources.resource_isdir(group, template_path):
            debug("Recursing on folder '{0}'".format(template_path))
            __place_template_folder(group, template_path, template_dst)
        else:
            try:
                debug("Placing template '{0}'".format(template_path))
                template = pkg_resources.resource_string(group, template_path)
                template_abs_path = pkg_resources.resource_filename(group, template_path)
            except IOError as err:
                error("Failed to load template "
                      "'{0}': {1}".format(template_file, str(err)), exit=True)
            if not os.path.exists(dst):
                os.makedirs(dst)
            if os.path.exists(template_dst):
                debug("Removing existing file '{0}'".format(template_dst))
                os.remove(template_dst)
            with open(template_dst, 'w') as f:
                if not isinstance(template, str):
                    template = template.decode('utf-8')
                f.write(template)
            shutil.copystat(template_abs_path, template_dst)


def place_template_files(path):
    info(fmt("@!@{bf}==>@| Placing templates files in the 'arch' folder."))
    arch_path = os.path.join(path, 'arch')
    # Create/Clean the arch folder
    if not os.path.exists(arch_path):
        os.makedirs(arch_path)
    # Place template files
    group = 'bloom.generators.arch'
    __place_template_folder(group, 'templates', arch_path)


def summarize_dependency_mapping(data, deps, build_deps, resolved_deps):
    if len(deps) == 0 and len(build_deps) == 0:
        return
    info("Package '" + data['Package'] + "' has dependencies:")
    header = "  " + ansi('boldoff') + ansi('ulon') + \
             "rosdep key           => " + data['Distribution'] + \
             " key" + ansi('reset')
    template = "  " + ansi('cyanf') + "{0:<20} " + ansi('purplef') + \
               "=> " + ansi('cyanf') + "{1}" + ansi('reset')
    if len(deps) != 0:
        info(ansi('purplef') + "Run Dependencies:" +
             ansi('reset'))
        info(header)
        for key in [d.name for d in deps]:
            info(template.format(key, resolved_deps[key]))
    if len(build_deps) != 0:
        info(ansi('purplef') +
             "Build and Build Tool Dependencies:" + ansi('reset'))
        info(header)
        for key in [d.name for d in build_deps]:
            info(template.format(key, resolved_deps[key]))


def format_depends(depends, resolved_deps):
    versions = {
        'version_lt': '<',
        'version_lte': '<=',
        'version_eq': '=',
        'version_gte': '>=',
        'version_gt': '>'
    }
    formatted = []
    for d in depends:
        for resolved_dep in resolved_deps[d.name]:
            version_depends = [k
                               for k in versions.keys()
                               if getattr(d, k, None) is not None]
            if not version_depends:
                formatted.append(resolved_dep)
            else:
                for v in version_depends:
                    formatted.append("{0}{1}{2}".format(
                        resolved_dep, versions[v], getattr(d, v)))
    return formatted


def missing_dep_resolver(key, peer_packages):
    if key in peer_packages:
        return [sanitize_package_name(key)]
    return default_fallback_resolver(key, peer_packages)


def generate_substitutions_from_package(
    package,
    os_name,
    os_version,
    ros_distro,
    installation_prefix='/usr',
    pkgrel=0,
    peer_packages=None,
    releaser_history=None,
    fallback_resolver=None
):
    tracks_dict = get_tracks_dict_raw()
    peer_packages = peer_packages or []
    data = {}
    # Name, Version, Description
    data['Name'] = package.name
    data['Version'] = package.version
    data['Description'] = archify_string(package.description)
    # License
    if not package.licenses or not package.licenses[0]:
        error("No license set for package '{0}', aborting.".format(package.name), exit=True)
    data['Licenses'] = package.licenses
    # Websites
    websites = [str(url) for url in package.urls if url.type == 'website']
    data['Homepage'] = websites[0] if websites else ''
    if data['Homepage'] == '':
        warning("No homepage set")
    # Package Release Number
    # Bloom's release number starts at 0 however Arch Linux expects it to starts at 1 by convention.
    data['Pkgrel'] = str(int(pkgrel)+1)
    # Package name
    data['Package'] = sanitize_package_name(package.name)
    # Installation prefix
    data['InstallationPrefix'] = installation_prefix

    # Resolve dependencies
    depends = package.run_depends + package.buildtool_export_depends
    build_depends = package.build_depends + package.buildtool_depends + package.test_depends
    unresolved_keys = depends + build_depends + package.replaces + package.conflicts
    # The installer key is not considered here, but it is checked when the keys are checked before this
    resolved_deps = resolve_dependencies(unresolved_keys, os_name,
                                         os_version, ros_distro,
                                         peer_packages + [d.name for d in package.replaces + package.conflicts],
                                         fallback_resolver)
    data['Depends'] = sorted(
        set(format_depends(depends, resolved_deps))
    )
    data['BuildDepends'] = sorted(
        set(format_depends(build_depends, resolved_deps))
    )
    data['Replaces'] = sorted(
        set(format_depends(package.replaces, resolved_deps))
    )
    data['Conflicts'] = sorted(
        set(format_depends(package.conflicts, resolved_deps))
    )
    # Set the distribution
    data['Distribution'] = os_version
    # Use the time stamp to set the date strings
    stamp = datetime.datetime.now(tz.tzlocal())
    data['Date'] = stamp.strftime('%a %b %d %Y')
    data['ROSDistribution'] = ros_distro
    # Maintainers
    maintainers = []
    for m in package.maintainers:
        maintainers.append(str(m))
    data['Maintainer'] = maintainers[0]
    data['Maintainers'] = ', '.join(maintainers)
    # Changelog
    if releaser_history:
        sorted_releaser_history = sorted(releaser_history,
                                         key=lambda k: LooseVersion(k), reverse=True)
        sorted_releaser_history = sorted(sorted_releaser_history,
                                         key=lambda k: strptime(releaser_history.get(k)[0], '%a %b %d %Y'),
                                         reverse=True)
        changelogs = [(v, releaser_history[v]) for v in sorted_releaser_history]
    else:
        # Ensure at least a minimal changelog
        changelogs = []
    print("Version!",package.version)
    if package.version + '-' + str(pkgrel) not in [x[0] for x in changelogs]:
        changelogs.insert(0, (
            package.version + '-' + str(pkgrel), (
                data['Date'],
                package.maintainers[0].name,
                package.maintainers[0].email
            )
        ))
    data['changelogs'] = changelogs
    # Summarize dependencies
    summarize_dependency_mapping(data, depends, build_depends, resolved_deps)

    def convertToUnicode(obj):
        if sys.version_info.major == 2:
            if isinstance(obj, str):
                return unicode(obj.decode('utf8'))
            elif isinstance(obj, unicode):
                return obj
        else:
            if isinstance(obj, bytes):
                return str(obj.decode('utf8'))
            elif isinstance(obj, str):
                return obj
        if isinstance(obj, list):
            for i, val in enumerate(obj):
                obj[i] = convertToUnicode(val)
            return obj
        elif isinstance(obj, type(None)):
            return None
        elif isinstance(obj, tuple):
            obj_tmp = list(obj)
            for i, val in enumerate(obj_tmp):
                obj_tmp[i] = convertToUnicode(obj_tmp[i])
            return tuple(obj_tmp)
        elif isinstance(obj, int):
            return obj
        elif isinstance(obj, int):
            return obj
        raise RuntimeError('need to deal with type %s' % (str(type(obj))))

    for item in data.items():
        data[item[0]] = convertToUnicode(item[1])

    return data


def __process_template_folder(path, subs):
    items = os.listdir(path)
    processed_items = []
    for item in list(items):
        item = os.path.abspath(os.path.join(path, item))
        if os.path.basename(item) in ['.', '..', '.git', '.svn']:
            continue
        if os.path.isdir(item):
            sub_items = __process_template_folder(item, subs)
            processed_items.extend([os.path.join(item, s) for s in sub_items])
        if not item.endswith(TEMPLATE_EXTENSION):
            continue
        with open(item, 'r') as f:
            template = f.read()
        # Remove extension
        template_path = item[:-len(TEMPLATE_EXTENSION)]
        # Expand template
        info("Expanding '{0}' -> '{1}'".format(
            os.path.relpath(item),
            os.path.relpath(template_path)))
        result = em.expand(template, **subs)
        # Write the result
        with open(template_path, 'w') as f:
            f.write(result)
        # Copy the permissions
        shutil.copymode(item, template_path)
        processed_items.append(item)
    return processed_items


def process_template_files(path, subs):
    info(fmt("@!@{bf}==>@| In place processing templates in 'arch' folder."))
    arch_dir = os.path.join(path, 'arch')
    if not os.path.exists(arch_dir):
        sys.exit("No arch directory found at '{0}', cannot process templates."
                 .format(arch_dir))
    return __process_template_folder(arch_dir, subs)


def match_branches_with_prefix(prefix, get_branches, prune=False):
    debug("match_branches_with_prefix(" + str(prefix) + ", " +
          str(get_branches()) + ")")
    branches = []
    # Match branches
    existing_branches = get_branches()
    for branch in existing_branches:
        if branch.startswith('remotes/origin/'):
            branch = branch.split('/', 2)[-1]
        if branch.startswith(prefix):
            branches.append(branch)
    branches = list(set(branches))
    if prune:
        # Prune listed branches by packages in latest upstream
        with inbranch('upstream'):
            pkg_names, version, pkgs_dict = get_package_data('upstream')
            for branch in branches:
                if branch.split(prefix)[-1].strip('/') not in pkg_names:
                    branches.remove(branch)
    return branches


def get_package_from_branch(branch):
    with inbranch(branch):
        try:
            package_data = get_package_data(branch)
        except SystemExit:
            return None
        if type(package_data) not in [list, tuple]:
            # It is a ret code
            ArchGenerator.exit(package_data)
    names, version, packages = package_data
    if type(names) is list and len(names) > 1:
        ArchGenerator.exit(
            "Arch generator does not support generating "
            "from branches with multiple packages in them, use "
            "the release generator first to split packages into "
            "individual branches.")
    if type(packages) is dict:
        return list(packages.values())[0]


def archify_string(value):
    markup_remover = re.compile(r'\"\\')
    value = markup_remover.sub('', value)
    value = re.sub('\s+', ' ', value)
    value = value.strip()
    return value


def sanitize_package_name(name):
    return name.replace('_', '-')


class ArchGenerator(BloomGenerator):
    title = 'arch'
    description = "Generates PKGBUILDs from the catkin meta data"
    has_run_rosdep = False
    default_install_prefix = '/usr'
    rosdistro = os.environ.get('ROS_DISTRO', 'indigo')

    def prepare_arguments(self, parser):
        # Add command line arguments for this generator
        add = parser.add_argument
        add('-i', '--pkgrel', help="PKGBUILD release number", default='0')
        add('-p', '--prefix', required=True,
            help="branch prefix to match, and from which create PKGBUILDs"
                 " hint: if you want to match 'release/foo' use 'release'")
        add('-a', '--match-all', default=False, action="store_true",
            help="match all branches with the given prefix, "
                 "even if not in current upstream")
        add('--distros', nargs='+', required=False, default=[],
            help='A list of PKGBUILD (archlinux) distros to generate for')
        add('--install-prefix', default=None,
            help="overrides the default installation prefix (/usr)")
        add('--os-name', default='arch',
            help="overrides os_name, set to 'arch' by default")

    def handle_arguments(self, args):
        self.interactive = args.interactive
        self.pkgrel = args.pkgrel
        self.os_name = args.os_name
        self.distros = args.distros
        if self.distros in [None, []]:
            index = rosdistro.get_index(rosdistro.get_index_url())
            distribution_file = rosdistro.get_distribution_file(index, self.rosdistro)
            if self.os_name not in distribution_file.release_platforms:
                warning("No platforms defined for os '{0}' in release file for the '{1}' distro."
                        "\nNot performing PKGBUILD generation."
                        .format(self.os_name, self.rosdistro))
                sys.exit(0)
            self.distros = distribution_file.release_platforms[self.os_name]
        self.install_prefix = args.install_prefix
        if args.install_prefix is None:
            self.install_prefix = self.default_install_prefix
        self.prefix = args.prefix
        self.branches = match_branches_with_prefix(self.prefix, get_branches, prune=not args.match_all)
        if len(self.branches) == 0:
            error(
                "No packages found, check your --prefix or --src arguments.",
                exit=True
            )
        self.packages = {}
        self.tag_names = {}
        self.names = []
        self.branch_args = []
        self.arch_branches = []
        for branch in self.branches:
            package = get_package_from_branch(branch)
            if package is None:
                # This is an ignored package
                continue
            self.packages[package.name] = package
            self.names.append(package.name)
            args = self.generate_branching_arguments(package, branch)
            # First branch is arch/[<rosdistro>/]<package>
            self.arch_branches.append(args[0][0])
            self.branch_args.extend(args)

    def summarize(self):
        info("Generating source PKGBUILDs for the packages: " + str(self.names))
        info("Arch Package Release: " + str(self.pkgrel))
        info("Arch Distributions: " + str(self.distros))

    def get_branching_arguments(self):
        return self.branch_args

    def update_rosdep(self):
        update_rosdep()
        self.has_run_rosdep = True

    def _check_all_keys_are_valid(self, peer_packages):
        keys_to_resolve = []
        key_to_packages_which_depends_on = collections.defaultdict(list)
        keys_to_ignore = set()
        for package in self.packages.values():
            depends = package.run_depends + package.buildtool_export_depends
            build_depends = package.build_depends + package.buildtool_depends + package.test_depends
            unresolved_keys = depends + build_depends + package.replaces + package.conflicts
            keys_to_ignore = keys_to_ignore.union(package.replaces + package.conflicts)
            keys = [d.name for d in unresolved_keys]
            keys_to_resolve.extend(keys)
            for key in keys:
                key_to_packages_which_depends_on[key].append(package.name)

        os_name = self.os_name
        rosdistro = self.rosdistro
        all_keys_valid = True
        for key in sorted(set(keys_to_resolve)):
            for os_version in self.distros:
                try:
                    extended_peer_packages = peer_packages + [d.name for d in keys_to_ignore]
                    rule, installer_key, default_installer_key = \
                        resolve_rosdep_key(key, os_name, os_version, rosdistro, extended_peer_packages,
                                           retry=False)
                    if rule is None:
                        continue
                    if installer_key != default_installer_key:
                        error("Key '{0}' resolved to '{1}' with installer '{2}', "
                              "which does not match the default installer '{3}'."
                              .format(key, rule, installer_key, default_installer_key))
                        BloomGenerator.exit(
                            "The Arch generator does not support dependencies "
                            "which are installed with the '{0}' installer."
                            .format(installer_key),
                            returncode=code.GENERATOR_INVALID_INSTALLER_KEY)
                except (GeneratorError, RuntimeError) as e:
                    print(fmt("Failed to resolve @{cf}@!{key}@| on @{bf}{os_name}@|:@{cf}@!{os_version}@| with: {e}")
                          .format(**locals()))
                    print(fmt("@{cf}@!{0}@| is depended on by these packages: ").format(key) +
                          str(list(set(key_to_packages_which_depends_on[key]))))
                    print(fmt("@{kf}@!<== @{rf}@!Failed@|"))
                    all_keys_valid = False
        return all_keys_valid

    def pre_modify(self):
        info("\nPre-verifying Arch dependency keys...")
        # Run rosdep update is needed
        if not self.has_run_rosdep:
            self.update_rosdep()

        peer_packages = [p.name for p in self.packages.values()]

        while not self._check_all_keys_are_valid(peer_packages):
            error("Some of the dependencies for packages in this repository could not be resolved by rosdep.")
            error("You can try to address the issues which appear above and try again if you wish.")
            try:
                if not maybe_continue(msg="Would you like to try again?"):
                    error("User aborted after rosdep keys were not resolved.")
                    sys.exit(code.GENERATOR_NO_ROSDEP_KEY_FOR_DISTRO)
            except (KeyboardInterrupt, EOFError):
                error("\nUser quit.", exit=True)
            update_rosdep()
            invalidate_view_cache()

        info("All keys are " + ansi('greenf') + "OK" + ansi('reset') + "\n")

        for package in self.packages.values():
            if not package.licenses or not package.licenses[0]:
                error("No license set for package '{0}', aborting.".format(package.name), exit=True)

    def pre_branch(self, destination, source):
        if destination in self.arch_branches:
            return
        # Run rosdep update is needed
        if not self.has_run_rosdep:
            self.update_rosdep()
        # Determine the current package being generated
        name = destination.split('/')[-1]
        distro = destination.split('/')[-2]
        # Retrieve the package
        package = self.packages[name]
        # Report on this package
        self.summarize_package(package, distro)

    def pre_rebase(self, destination):
        # Get the stored configs is any
        patches_branch = 'patches/' + destination
        config = self.load_original_config(patches_branch)
        if config is not None:
            curr_config = get_patch_config(patches_branch)
            if curr_config['parent'] == config['parent']:
                set_patch_config(patches_branch, config)

    def post_rebase(self, destination):
        name = destination.split('/')[-1]
        # Retrieve the package
        package = self.packages[name]
        # Handle differently if this is an arch vs distro branch
        if destination in self.arch_branches:
            info("Placing Arch template files into '{0}' branch."
                 .format(destination))
            # Then this is an arch branch
            # Place the raw template files
            self.place_template_files()
        else:
            # This is a distro specific arch branch
            # Determine the current package being generated
            distro = destination.split('/')[-2]

            # Create Arch packages for each distro
            with inbranch(destination):

                # To fit Arch Linux philosophy a bit better, we move all the source files into a subdirectory.
                # Arch Linux doesn't support source distribution through a subdirectory; therefore we should ideally compress the sources or provide a URL.
                # At this point in the generator, it is tricky to get the release URL. Furthermore it wouldn't fit bloom's patch mechanism very well.
                # To work around, we copy the sources to the $srcdir at the beginning inside the prepare() function.
                temp_dir = mkdtemp(dir='.')
                for item in os.listdir("."):
                    itemsrc = os.path.abspath(item)
                    if os.path.basename(itemsrc) in ['.', '..', '.git', '.svn', 'arch',os.path.basename(temp_dir)]:
                        continue
                    itemdst = os.path.abspath(os.path.join(temp_dir,item))
                    execute_command('git mv ' + itemsrc + ' ' + itemdst)
                execute_command('git mv ' + temp_dir + ' ' + name)
                execute_command('git commit --amend --no-edit')

                # Then generate the PKGBUILD
                data = self.generate_arch(package, distro)

                # And finally move the PKGBUILD to the root directory of the package.
                for item in os.listdir("arch"):
                    itemsrc = os.path.abspath(os.path.join("arch",item))
                    if os.path.basename(itemsrc) in ['.', '..', '.git', '.svn']:
                        continue
                    itemdst = os.path.abspath(item)
                    execute_command('git mv ' + itemsrc + ' ' + itemdst)
                execute_command('git commit --amend --no-edit')

                # Create the tag name for later
                self.tag_names[destination] = self.generate_tag_name(data)

        # Update the patch configs
        patches_branch = 'patches/' + destination
        config = get_patch_config(patches_branch)
        # Store it
        self.store_original_config(config, patches_branch)
        # Modify the base so import/export patch works
        current_branch = get_current_branch()
        if current_branch is None:
            error("Could not determine current branch.", exit=True)
        config['base'] = get_commit_hash(current_branch)
        # Set it
        set_patch_config(patches_branch, config)

    def post_patch(self, destination, color='bluef'):
        if destination in self.arch_branches:
            return
        # Tag after patches have been applied
        with inbranch(destination):
            # Tag
            tag_name = self.tag_names[destination]
            if tag_exists(tag_name):
                if self.interactive:
                    warning("Tag exists: " + tag_name)
                    warning("Do you wish to overwrite it?")
                    if not maybe_continue('y'):
                        error("Answered no to continue, aborting.", exit=True)
                else:
                    warning("Overwriting tag: " + tag_name)
            else:
                info("Creating tag: " + tag_name)
            execute_command('git tag -f ' + tag_name)
        # Report of success
        name = destination.split('/')[-1]
        package = self.packages[name]
        distro = destination.split('/')[-2]
        info(ansi(color) + "####" + ansi('reset'), use_prefix=False)
        info(
            ansi(color) + "#### " + ansi('greenf') + "Successfully" +
            ansi(color) + " generated '" + ansi('boldon') + distro +
            ansi('boldoff') + "' Arch for package"
            " '" + ansi('boldon') + package.name + ansi('boldoff') + "'" +
            " at version '" + ansi('boldon') + package.version +
            "-" + str(self.pkgrel) + ansi('boldoff') + "'" +
            ansi('reset'),
            use_prefix=False
        )
        info(ansi(color) + "####\n" + ansi('reset'), use_prefix=False)

    def store_original_config(self, config, patches_branch):
        with inbranch(patches_branch):
            with open('arch.store', 'w+') as f:
                f.write(json.dumps(config))
            execute_command('git add arch.store')
            if has_changes():
                execute_command('git commit -m "Store original patch config"')

    def load_original_config(self, patches_branch):
        config_store = show(patches_branch, 'arch.store')
        if config_store is None:
            return config_store
        return json.loads(config_store)

    def place_template_files(self, arch_dir='arch'):
        # Create/Clean the arch folder
        if os.path.exists(arch_dir):
            if self.interactive:
                warning("arch directory exists: " + arch_dir)
                warning("Do you wish to overwrite it?")
                if not maybe_continue('y'):
                    error("Answered no to continue, aborting.", exit=True)
            else:
                warning("Overwriting arch directory: " + arch_dir)
            execute_command('git rm -rf ' + arch_dir)
            execute_command('git commit -m "Clearing previous arch folder"')
            if os.path.exists(arch_dir):
                shutil.rmtree(arch_dir)
        # Use generic place template files command
        place_template_files('.')
        # Commit results
        execute_command('git add ' + arch_dir)
        execute_command('git commit -m "Placing arch template files"')

    def get_releaser_history(self):
        # Assumes that this is called in the target branch
        patches_branch = 'patches/' + get_current_branch()
        raw = show(patches_branch, 'releaser_history.json')
        return None if raw is None else json.loads(raw)

    def set_releaser_history(self, history):
        # Assumes that this is called in the target branch
        patches_branch = 'patches/' + get_current_branch()
        debug("Writing release history to '{0}' branch".format(patches_branch))
        with inbranch(patches_branch):
            with open('releaser_history.json', 'w') as f:
                f.write(json.dumps(history))
            execute_command('git add releaser_history.json')
            if has_changes():
                execute_command('git commit -m "Store releaser history"')

    def get_subs(self, package, arch_distro, releaser_history=None):
        return generate_substitutions_from_package(
            package,
            self.os_name,
            arch_distro,
            self.rosdistro,
            self.install_prefix,
            self.pkgrel,
            [p.name for p in self.packages.values()],
            releaser_history=releaser_history,
            fallback_resolver=missing_dep_resolver
        )

    def generate_arch(self, package, arch_distro, arch_dir='arch'):
        info("Generating PKGBUILD for {0}...".format(arch_distro))
        # Try to retrieve the releaser_history
        releaser_history = self.get_releaser_history()
        # Generate substitution values
        subs = self.get_subs(package, arch_distro, releaser_history)
        # Use subs to create and store releaser history
        self.set_releaser_history(dict(subs['changelogs']))
        # Template files
        template_files = process_template_files('.', subs)
        # Remove any residual template files
        info('git rm -rf ' + ' '.join(template_files))
        execute_command('git rm -rf ' + ' '.join(template_files))
        # Add changes to the arch folder
        execute_command('git add ' + arch_dir)
        # Commit changes
        execute_command('git commit -m "Generated PKGBUILD file for ' +
                        arch_distro + '"')
        # Return the subs for other use
        return subs

    def generate_tag_name(self, data):
        tag_name = '{Package}-{Version}-{Pkgrel}_{Distribution}'
        tag_name = 'arch/' + tag_name.format(**data)
        return tag_name

    def generate_branching_arguments(self, package, branch):
        n = package.name
        # arch branch
        arch_branch = 'arch/' + n
        # Branch first to the arch branch
        args = [[arch_branch, branch, False]]
        # Then for each Arch distro, branch from the base arch branch
        args.extend([
            ['arch/' + d + '/' + n, arch_branch, False] for d in self.distros
        ])
        return args

    def summarize_package(self, package, distro, color='bluef'):
        info(ansi(color) + "\n####" + ansi('reset'), use_prefix=False)
        info(
            ansi(color) + "#### Generating '" + ansi('boldon') + distro +
            ansi('boldoff') + "' Arch for package"
            " '" + ansi('boldon') + package.name + ansi('boldoff') + "'" +
            " at version '" + ansi('boldon') + package.version +
            "-" + str(self.pkgrel) + ansi('boldoff') + "'" +
            ansi('reset'),
            use_prefix=False
        )
        info(ansi(color) + "####" + ansi('reset'), use_prefix=False)
