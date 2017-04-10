"""

"""


import nipype.pipeline.engine as npe
import nipype.interfaces.io as nio
import nipype.interfaces.spm as spm
import nipype.interfaces.fsl as fsl
import nipype.interfaces.utility as nutil
import clinica.pipeline.engine as cpe
from clinica.utils.io import zip_nii, unzip_nii


def filter_data(file_list):
    new_file_list = filter(None, file_list)
    return new_file_list

class BrainExtractionWorkflow(npe.Workflow):

    def __init__(self, name, base_dir=None):

        super(BrainExtractionWorkflow, self).__init__(name, base_dir)

        # Segmentation
        # ============
        seg_node = npe.MapNode(name="Segmentation",
                            iterfield="data",
                            interface=spm.Segment())
        seg_node.inputs.gm_output_type = [False, False, True]
        seg_node.inputs.wm_output_type = [False, False, True]
        seg_node.inputs.csf_output_type = [False, False, True]
        add1_node = npe.MapNode(name="AddGMWM",
                                iterfield=["in_file", "operand_file"],
                             interface=fsl.BinaryMaths())
        add1_node.inputs.operation = 'add'
        add2_node = npe.MapNode(name="AddGMWMCSF",
                                iterfield=["in_file", "operand_file"],
                             interface=fsl.BinaryMaths())
        add2_node.inputs.operation = 'add'
        dil_node = npe.MapNode(name="Dilate",
                               iterfield="in_file",
                            interface=fsl.DilateImage())
        dil_node.inputs.operation = 'mean'
        ero_node = npe.MapNode(name="Erode",
                               iterfield="in_file",
                            interface=fsl.ErodeImage())
        thre_node = npe.MapNode(name="Threshold",
                                iterfield="in_file",
                            interface=fsl.Threshold())
        thre_node.inputs.thresh = 0.5
        fill_node = npe.MapNode(name="Fill",
                                iterfield="in_file",
                            interface=fsl.UnaryMaths())
        fill_node.inputs.operation = 'fillh'
        mask_node = npe.MapNode(name="ApplyMask",
                            iterfield=["in_file", "mask_file"],
                            interface=fsl.ApplyMask())
        mask_node.inputs.output_type = 'NIFTI'

        self.connect([
            (seg_node   , add1_node   , [('native_gm_image' , 'in_file'     )]),
            (seg_node   , add1_node   , [('native_wm_image' , 'operand_file')]),
            (seg_node   , add2_node   , [('native_csf_image', 'in_file'     )]),
            (add1_node  , add2_node   , [('out_file'        , 'operand_file')]),
            (add2_node  , dil_node    , [('out_file'        , 'in_file'     )]),
            (dil_node   , ero_node    , [('out_file'        , 'in_file'     )]),
            (ero_node   , thre_node   , [('out_file'        , 'in_file'     )]),
            (thre_node  , fill_node   , [('out_file'        , 'in_file'     )]),
            (fill_node  , mask_node   , [('out_file'        , 'mask_file'   )]),
        ])


class FMRIPreprocessing(cpe.Pipeline):
    """Create fMRI preprocessing pipeline object.

    TODO:
        - [x] Replace reg_node target image by the brain only using c1 + c2 + c3 dilated-eroded-filled.
        - [x] Develop SPM Realign and Unwarp wrapper and integrate it.
        - [x] Develop SPM Fieldmap Calculation Tool wrapper and integrate it.
        - [ ] Replace standard DataGrabber by a BIDS tree finder.
        - [ ] Add support of gzipped nifti inputs.

    Args:
        input_dir: A BIDS directory.
        output_dir: An empty output directory where CAPS structured data will be written.
        subjects_sessions_list: The Subjects-Sessions list file (in .tsv format).

    Returns:
        A nipype workflow object containing the full fMRI preprocessing pipeline.

    Raises:
        IOError:

    Example:
        >>> from clinica.pipeline.fmri.preprocessing import FMRIPreprocessing
        >>> pipeline = FMRIPreprocessing('~/MYDATASET_BIDS', '~/MYDATASET_CAPS')
        >>> pipeline.parameters = {
        >>>     'num_slices': 45,
        >>>     'time_repetition': 2.4,
        >>>     'echo_times': [5.19, 7.65],
        >>>     'blip_direction': 1,
        >>>     'total_readout_time': 15.6799
        >>> }
        >>> pipeline.base_dir = '/tmp/'
        >>> pipeline.run()
    """

    @cpe.postset('is_built', True)
    def build(self):

        # NOTE: The data grabber is going to work only when nipype team will have solved this issue
        #   (https://github.com/nipy/nipype/issues/1783), I removed l.1183-1184.
        # NOTE: The Fieldmap node is still under revision as a pull request
        # NOTE: The RealingUnwarp node is still under revision as a pull request
        # TODO: Parametrize input templates for the DataGrabbing node.
        # TODO: [DONE] Use spm.RealignAndUnwarp instead of spm.Realign.

        # FieldMap calculation
        # ====================
        fm_node = npe.MapNode(name="FieldMapCalculation",
                              iterfield=['phase', 'magnitude', 'epi'],
                              interface=spm.FieldMap())
        fm_node.inputs.magnitude = self.bids_layout.get(return_type='file', type='magnitude1', extensions='nii')
        fm_node.inputs.phase = self.bids_layout.get(return_type='file', type='phasediff', extensions='nii')
        fm_node.inputs.epi = self.bids_layout.get(return_type='file', type='bold', extensions='nii')
        fm_node.inputs.et = self.parameters['echo_times']
        fm_node.inputs.blipdir = self.parameters['blip_direction']
        fm_node.inputs.tert = self.parameters['total_readout_time']

        # Slice timing correction
        # =======================
        st_node = npe.Node(name="SliceTimingCorrection",
                           interface=spm.SliceTiming())
        st_node.inputs.in_files = self.bids_layout.get(return_type='file', type='bold', extensions='nii')
        st_node.inputs.time_repetition = self.parameters['time_repetition']
        st_node.inputs.slice_order = range(1, self.parameters['num_slices']+1)
        st_node.inputs.num_slices = self.parameters['num_slices']
        st_node.inputs.ref_slice = self.parameters['num_slices'] / 2
        st_node.inputs.time_acquisition = self.parameters['time_repetition'] - self.parameters['time_repetition'] \
                                                                               / float(self.parameters['num_slices'])

        # Motion correction and unwarping
        # ===============================
        mc_node = npe.MapNode(name="MotionCorrectionUnwarping",
                              iterfield=["scans", "pmscan"],
                              interface=spm.RealignUnwarp())
        mc_node.inputs.register_to_mean = True
        mc_node.inputs.write_mask = False

        # Brain extraction
        # ================
        bet_node = BrainExtractionWorkflow(name="BrainExtraction")
        bet_node.get_node('Segmentation').inputs.data = self.bids_layout.get(return_type='file', run='[1]', type='T1w', extensions='nii')
        bet_node.get_node('ApplyMask').inputs.in_file = self.bids_layout.get(return_type='file', run='[1]', type='T1w', extensions='nii')

        # Registration
        # ============
        reg_node = npe.MapNode(interface=spm.Coregister(),
                               iterfield=["apply_to_files", "source", "target"],
                               name="Registration")

        # Normalization
        # =============
        norm_node = npe.MapNode(interface=spm.Normalize12(),
                                iterfield=['image_to_align', 'apply_to_files'],
                                name='Normalization')
        norm_node.inputs.image_to_align = self.bids_layout.get(return_type='file', run='[1]', type='T1w', extensions='nii')

        # Smoothing
        # =========
        smooth_node = npe.MapNode(interface=spm.Smooth(),
                                  iterfield=['in_files'],
                                  name='Smoothing')

        # Zipping
        # =======
        zip_node = npe.MapNode(name='Zipping',
                               iterfield=['in_file'],
                               interface=nutil.Function(input_names=['in_file'],
                                                        output_names=['out_file'],
                                                        function=zip_nii))

        # Connection
        # ==========
        self.connect([
            # FieldMap calculation
            # (dg_node,        fm_node,    [('mag1_nii',                'magnitude')]),
            # (dg_node,        fm_node,    [('pdif_nii',                    'phase')]),
            # (dg_node,        fm_node,    [('bold_nii',                      'epi')]),
            # Brain extraction
            # (dg_node,       bet_node,    [('t1_nii',          'Segmentation.data')]),
            # (dg_node,       bet_node,    [('t1_nii',          'ApplyMask.in_file')]),
            # Slice timing correction
            # (dg_node,        st_node,    [('bold_nii',                 'in_files')]),
            # Motion correction and unwarping
            (st_node,        mc_node,    [('timecorrected_files',         'scans')]),
            (fm_node,        mc_node,    [('vdm',                        'pmscan')]),
            # Registration
            (mc_node,       reg_node,    [('mean_image',                 'source')]),
            (mc_node,       reg_node,    [('runwarped_files',    'apply_to_files')]),
            (bet_node,      reg_node,    [('ApplyMask.out_file',         'target')]),
            # Normalization
            # (dg_node,      norm_node,    [('t1_nii',             'image_to_align')]),
            (reg_node,     norm_node,    [('coregistered_files', 'apply_to_files')]),
            # Smoothing
            (norm_node,  smooth_node,    [('normalized_files',         'in_files')]),
        ])

        self.write_graph()

        return self