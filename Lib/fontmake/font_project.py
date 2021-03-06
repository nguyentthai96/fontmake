# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import print_function, division, absolute_import
from __future__ import unicode_literals

import glob
import logging
import os
import plistlib
import re
import tempfile
from io import open

from cu2qu.pens import ReverseContourPen
from cu2qu.ufo import font_to_quadratic, fonts_to_quadratic
from defcon import Font
from fontTools import subset
from fontTools.misc.py23 import tobytes, UnicodeIO
from fontTools.misc.loggingTools import configLogger, Timer
from fontTools.misc.transform import Transform
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools import varLib
from ufo2ft import compileOTF, compileTTF
from ufo2ft.makeotfParts import FeatureOTFCompiler

from fontmake.ttfautohint import ttfautohint

timer = Timer(logging.getLogger('fontmake'), level=logging.DEBUG)

PUBLIC_PREFIX = 'public.'
GLYPHS_PREFIX = 'com.schriftgestaltung.'


class FontProject:
    """Provides methods for building fonts."""

    @staticmethod
    def preprocess(glyphs_path):
        """Return Glyphs source with illegal glyph/class names changed."""

        with open(glyphs_path, 'r', encoding='utf-8') as fp:
            text = fp.read()
        names = set(re.findall('\n(?:glyph)?name = "(.+-.+)";\n', text))

        if names:
            num_names = len(names)
            printed_names = sorted(names)[:5]
            if num_names > 5:
                printed_names.append('...')
            print('Found %s glyph names containing hyphens: %s' % (
                num_names, ', '.join(printed_names)))
            print('Replacing all hyphens with periods.')

        for old_name in names:
            new_name = old_name.replace('-', '.')
            text = text.replace(old_name, new_name)
        return text

    def __init__(self, timing=False):
        if timing:
            configLogger(logger=timer.logger, level=logging.DEBUG)

    @timer()
    def build_master_ufos(self, glyphs_path, family_name=None):
        """Build UFOs and MutatorMath designspace from Glyphs source."""
        import glyphsLib

        master_dir = self._output_dir('ufo')
        instance_dir = self._output_dir('ufo', is_instance=True)
        return glyphsLib.build_masters(
            glyphs_path, master_dir, designspace_instance_dir=instance_dir,
            family_name=family_name)

    @timer()
    def remove_overlaps(self, ufos):
        """Remove overlaps in UFOs' glyphs' contours."""
        from booleanOperations import BooleanOperationManager

        manager = BooleanOperationManager()
        for ufo in ufos:
            print('>> Removing overlaps for ' + self._font_name(ufo))
            for glyph in ufo:
                contours = list(glyph)
                glyph.clearContours()
                manager.union(contours, glyph.getPointPen())

    @timer()
    def decompose_glyphs(self, ufos):
        """Move components of UFOs' glyphs to their outlines."""

        for ufo in ufos:
            print('>> Decomposing glyphs for ' + self._font_name(ufo))
            for glyph in ufo:
                self._deep_copy_contours(ufo, glyph, glyph, Transform())
                glyph.clearComponents()

    def _deep_copy_contours(self, ufo, parent, component, transformation):
        """Copy contours from component to parent, including nested components."""

        for nested in component.components:
            self._deep_copy_contours(
                ufo, parent, ufo[nested.baseGlyph],
                transformation.transform(nested.transformation))

        if component != parent:
            pen = TransformPen(parent.getPen(), transformation)

            # if the transformation has a negative determinant, it will reverse
            # the contour direction of the component
            xx, xy, yx, yy = transformation[:4]
            if xx * yy - xy * yx < 0:
                pen = ReverseContourPen(pen)

            component.draw(pen)

    @timer()
    def convert_curves(self, ufos, compatible=False, reverse_direction=True,
                       conversion_error=None):
        if compatible:
            fonts_to_quadratic(
                ufos, max_err_em=conversion_error,
                reverse_direction=reverse_direction, dump_stats=True)
        else:
            for ufo in ufos:
                print('>> Converting curves for ' + self._font_name(ufo))
                font_to_quadratic(
                    ufo, max_err_em=conversion_error,
                    reverse_direction=reverse_direction, dump_stats=True)

    def build_otfs(self, ufos, remove_overlaps=True, **kwargs):
        """Build OpenType binaries with CFF outlines."""

        print('\n>> Building OTFs')

        self.decompose_glyphs(ufos)
        if remove_overlaps:
            self.remove_overlaps(ufos)
        self.save_otfs(ufos, **kwargs)

    def build_ttfs(
            self, ufos, remove_overlaps=True, reverse_direction=True,
            conversion_error=None, **kwargs):
        """Build OpenType binaries with TrueType outlines."""

        print('\n>> Building TTFs')

        if remove_overlaps:
            self.remove_overlaps(ufos)
        self.convert_curves(ufos, reverse_direction=reverse_direction,
                            conversion_error=conversion_error)
        self.save_otfs(ufos, ttf=True, **kwargs)

    def build_interpolatable_ttfs(
            self, ufos, reverse_direction=True, conversion_error=None,
            **kwargs):
        """Build OpenType binaries with interpolatable TrueType outlines."""

        print('\n>> Building interpolation-compatible TTFs')

        self.convert_curves(ufos, compatible=True,
                            reverse_direction=reverse_direction,
                            conversion_error=conversion_error)
        self.save_otfs(ufos, ttf=True, interpolatable=True, **kwargs)

    @timer()
    def save_otfs(
            self, ufos, ttf=False, is_instance=False, interpolatable=False,
            mti_paths=None, use_afdko=False, autohint=None, subset=None,
            use_production_names=None, subroutinize=False):
        """Build OpenType binaries from UFOs.

        Args:
            ufos: Font objects to compile.
            ttf: If True, build fonts with TrueType outlines and .ttf extension.
            is_instance: If output fonts are instances, for generating paths.
            interpolatable: If output is interpolatable, for generating paths.
            mti_source: Dictionary mapping postscript full names to dictionaries
                mapping layout table tags to MTI source paths which should be
                compiled into those tables.
            use_afdko: If True, use AFDKO to compile feature source.
            autohint: Parameters to provide to ttfautohint. If not provided, the
                autohinting step is skipped.
            subset: Whether to subset the output according to data in the UFOs.
                If not provided, also determined by flags in the UFOs.
            use_production_names: Whether to use production glyph names in the
                output. If not provided, determined by flags in the UFOs.
            subroutinize: If True, subroutinize CFF outlines in output.
        """

        ext = 'ttf' if ttf else 'otf'
        fea_compiler = FDKFeatureCompiler if use_afdko else FeatureOTFCompiler
        otf_compiler = compileTTF if ttf else compileOTF

        for ufo in ufos:
            name = self._font_name(ufo)
            print('>> Saving %s for %s' % (ext.upper(), name))

            otf_path = self._output_path(ufo, ext, is_instance, interpolatable)
            if use_production_names is None:
                use_production_names = not ufo.lib.get(
                    GLYPHS_PREFIX + "Don't use Production Names")
            otf = otf_compiler(
                ufo, featureCompilerClass=fea_compiler,
                mtiFeaFiles=mti_paths[name] if mti_paths is not None else None,
                glyphOrder=ufo.lib[PUBLIC_PREFIX + 'glyphOrder'],
                useProductionNames=use_production_names,
                convertCubics=False, optimizeCff=subroutinize)
            otf.save(otf_path)

            if subset is None:
                export_key = GLYPHS_PREFIX + 'Glyphs.Export'
                subset = ((GLYPHS_PREFIX + 'Keep Glyphs') in ufo.lib or
                          any(glyph.lib.get(export_key, True) is False
                              for glyph in ufo))
            if subset:
                self.subset_otf_from_ufo(otf_path, ufo)

            if ttf and autohint is not None:
                hinted_otf_path = self._output_path(
                    ufo, ext, is_instance, interpolatable, autohinted=True)
                ttfautohint(otf_path, hinted_otf_path, args=autohint)

    def subset_otf_from_ufo(self, otf_path, ufo):
        """Subset a font using export flags set by glyphsLib."""

        keep_glyphs = set(ufo.lib.get(GLYPHS_PREFIX + 'Keep Glyphs', []))

        include = []
        ufo_order = [glyph_name
                     for glyph_name in ufo.lib[PUBLIC_PREFIX + 'glyphOrder']
                     if glyph_name in ufo]
        for old_name, new_name in zip(
                ufo_order,
                TTFont(otf_path).getGlyphOrder()):
            glyph = ufo[old_name]
            if ((keep_glyphs and old_name not in keep_glyphs) or
                not glyph.lib.get(GLYPHS_PREFIX + 'Glyphs.Export', True)):
                continue
            include.append(new_name)

        # copied from nototools.subset
        opt = subset.Options()
        opt.name_IDs = ['*']
        opt.name_legacy = True
        opt.name_languages = ['*']
        opt.layout_features = ['*']
        opt.notdef_outline = True
        opt.recalc_bounds = True
        opt.recalc_timestamp = True
        opt.canonical_order = True

        opt.glyph_names = True

        font = subset.load_font(otf_path, opt, lazy=False)
        subsetter = subset.Subsetter(options=opt)
        subsetter.populate(glyphs=include)
        subsetter.subset(font)
        subset.save_font(font, otf_path, opt)

    def run_from_glyphs(
            self, glyphs_path, preprocess=True, family_name=None, **kwargs):
        """Run toolchain from Glyphs source.

        Args:
            glyphs_path: Path to source file.
            preprocess: If True, check source file for un-compilable content.
            family_name: If provided, uses this family name in the output.
            kwargs: Arguments passed along to run_from_designspace.
        """

        if preprocess:
            print('>> Checking Glyphs source for illegal glyph names')
            glyphs_source = self.preprocess(glyphs_path)
            glyphs_path = UnicodeIO(glyphs_source)

        print('>> Building master UFOs and designspace from Glyphs source')
        _, designspace_path, instance_data = self.build_master_ufos(
            glyphs_path, family_name)
        self.run_from_designspace(
            designspace_path, instance_data=instance_data, **kwargs)

    def run_from_designspace(
            self, designspace_path, interpolate=False,
            masters_as_instances=False, instance_data=None, **kwargs):
        """Run toolchain from a MutatorMath design space document.

        Args:
            designspace_path: Path to designspace document.
            interpolate: If True output instance fonts, otherwise just masters.
            masters_as_instances: If True, output master fonts as instances.
            instance_data: Data to be applied to instance UFOs, as returned from
                glyphsLib's parsing function.
            kwargs: Arguments passed along to run_from_ufos.
        """

        from glyphsLib.interpolation import apply_instance_data
        from mutatorMath.ufo import build as build_designspace
        from mutatorMath.ufo.document import DesignSpaceDocumentReader

        ufos = []
        if not interpolate or masters_as_instances:
            reader = DesignSpaceDocumentReader(designspace_path, ufoVersion=3)
            ufos.extend(reader.getSourcePaths())
        if interpolate:
            print('>> Interpolating master UFOs from designspace')
            results = build_designspace(
                designspace_path, outputUFOFormatVersion=3)
            for result in results:
                if instance_data is not None:
                    ufos.extend(apply_instance_data(instance_data))
        self.run_from_ufos(
            ufos, designspace_path=designspace_path,
            is_instance=(interpolate or masters_as_instances), **kwargs)

    def run_from_ufos(
            self, ufos, output=(), designspace_path=None, mti_source=None,
            remove_overlaps=True, reverse_direction=True, conversion_error=None,
            **kwargs):
        """Run toolchain from UFO sources.

        Args:
            ufos: List of UFO sources, as either paths or opened objects.
            output: List of output formats to generate.
            designspace_path: Path to a MutatorMath designspace, used to
                generate variable font if requested.
            mti_source: MTI layout source to be parsed and passed to save_otfs.
            remove_overlaps: If True, remove overlaps in glyph shapes.
            reverse_direction: If True, reverse contour directions when
                compiling TrueType outlines.
            conversion_error: Error to allow when converting cubic CFF contours
                to quadratic TrueType contours.
            kwargs: Arguments passed along to save_otfs.

        Raises:
            TypeError: 'variable' specified in output formats but designspace
                path not provided.
        """

        if set(output) == set(['ufo']):
            return
        if hasattr(ufos[0], 'path'):
            ufo_paths = [ufo.path for ufo in ufos]
        else:
            if isinstance(ufos, str):
                ufo_paths = glob.glob(ufos)
            else:
                ufo_paths = ufos
            ufos = [Font(path) for path in ufo_paths]

        mti_paths = None
        if mti_source:
            mti_paths = {}
            mti_paths = plistlib.readPlist(mti_source)
            src_dir = os.path.dirname(mti_source)
            for paths in mti_paths.values():
                for tag in paths.keys():
                    paths[tag] = os.path.join(src_dir, paths[tag])

        need_reload = False
        if 'otf' in output:
            self.build_otfs(
                ufos, remove_overlaps, mti_paths=mti_paths, **kwargs)
            need_reload = True

        if 'ttf' in output:
            if need_reload:
                ufos = [Font(path) for path in ufo_paths]
            self.build_ttfs(
                ufos, remove_overlaps, reverse_direction, conversion_error,
                mti_paths=mti_paths, **kwargs)
            need_reload = True

        if 'ttf-interpolatable' in output or 'variable' in output:
            if need_reload:
                ufos = [Font(path) for path in ufo_paths]
            self.build_interpolatable_ttfs(
                ufos, reverse_direction, conversion_error, mti_paths=mti_paths,
                **kwargs)

        if 'variable' in output:
            if designspace_path is None:
                raise TypeError('Need designspace to build variable font.')
            varLib.main((designspace_path,))

    def _font_name(self, ufo):
        return '%s-%s' % (ufo.info.familyName.replace(' ', ''),
                          ufo.info.styleName.replace(' ', ''))

    def _output_dir(self, ext, is_instance=False, interpolatable=False,
                    autohinted=False):
        """Generate an output directory."""

        dir_prefix = 'instance_' if is_instance else 'master_'
        dir_suffix = '_interpolatable' if interpolatable else ''
        output_dir = dir_prefix + ext + dir_suffix
        if autohinted:
            output_dir = os.path.join('autohinted', output_dir)
        return output_dir

    def _output_path(self, ufo, ext, is_instance=False, interpolatable=False,
                     autohinted=False):
        """Generate output path for a font file with given extension."""

        out_dir = self._output_dir(ext, is_instance, interpolatable, autohinted)
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        return os.path.join(out_dir, '%s.%s' % (self._font_name(ufo), ext))


class FDKFeatureCompiler(FeatureOTFCompiler):
    """An OTF compiler which uses the AFDKO to compile feature syntax."""

    def setupFile_featureTables(self):
        if self.mtiFeaFiles is not None:
            super(FDKFeatureCompiler, self).setupFile_featureTables()

        elif not self.features.strip():
            return

        import subprocess
        from fontTools.misc.py23 import tostr

        fd, outline_path = tempfile.mkstemp()
        os.close(fd)
        self.outline.save(outline_path)

        fd, feasrc_path = tempfile.mkstemp()
        os.close(fd)

        fd, fea_path = tempfile.mkstemp()
        os.write(fd, tobytes(self.features, encoding='utf-8'))
        os.close(fd)

        report = tostr(subprocess.check_output([
            "makeotf", "-o", feasrc_path, "-f", outline_path,
            "-ff", fea_path]))
        os.remove(outline_path)
        os.remove(fea_path)

        print(report)
        success = "Done." in report
        if success:
            feasrc = TTFont(feasrc_path)
            for table in ["GDEF", "GPOS", "GSUB"]:
                if table in feasrc:
                    self.outline[table] = feasrc[table]
            feasrc.close()

        os.remove(feasrc_path)
        if not success:
            raise ValueError("Feature syntax compilation failed.")
