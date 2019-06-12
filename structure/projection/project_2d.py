# coding:utf-8
import os
import ctypes as ct
import numpy as np
import limber
from gsl_wrappers import GSLSpline, NullSplineError, GSLSpline2d, BICUBIC
from cosmosis.datablock import names, option_section, BlockError
from enum34 import Enum
import re
import sys
import scipy.interpolate as interp
from pk2cl_tools import limber_integral, get_dlogchi, exact_integral, nearest_power_of_2
from kernel import TomoNzKernel
#from pk2cl import get_cl_exact, Kernel, get_dlogchi, resample_power

#for timing 
from timeit import default_timer as timer
from datetime import timedelta

class Power3D(object):
    """
    Class representing the 3D power spectrum that enters the Limber calculation.
    Most Power spectra are source-specific, like intrinsic alignments and galaxy
    density.  Others are generic, like matter_power (linear and nonlinear)
    """
    # Must specify these:
    # section = "?????"
    # source_specific = None
    def __init__(self, suffix=""):
        if self.source_specific:
            self.section_name = self.section + suffix
            try:
                self.lin_section_name = self.lin_section + suffix
            except AttributeError:
                self.lin_section_name = None
        else:
            self.section_name = self.section
            try:
                self.lin_section_name = self.lin_section
            except AttributeError:
                self.lin_section_name = None

        self.chi_logk_spline = None
        self.lin_z0_logk_spline = None
        self.lin_growth_spline = None
        self.sublin_spline = None

    def __hash__(self):
        return hash(self.section_name)

    def load_from_block(self, block, chi_of_z):
        z, k, pk = block.get_grid( self.section_name, "z", "k_h", "p_k" )
        self.z_vals = z
        self.chi_vals = chi_of_z(z)
        self.k_vals = k
        self.logk_vals = np.log(k)
        self.pk_vals = pk

    def set_chi_logk_spline(self):
        self.chi_logk_spline = interp.RectBivariateSpline(self.chi_vals, self.logk_vals, self.pk_vals)

    def set_nonlimber_splines(self, block, chi_of_z, k_growth=1.e-3):
        #For the exact projection integral we need
        #i) A P_lin(k,z=0) python spline (this is added to self.power_lin_z0)
        #ii) A D_lin(chi) python spline (this is added to self.growth_lin)
        #iii) A (P_nl - P_lin) 2d (chi,k) spline to feed into the Limber calculation
        z_lin, k_lin, P_lin = block.get_grid(self.lin_section_name, "z", "k_h", "p_k")
        chi_lin = chi_of_z(z_lin)
        
        #Get z=0 slice and set up spline
        P_lin_z0 = P_lin[0,:] 
        self.lin_z0_logk_spline = interp.InterpolatedUnivariateSpline(np.log(k_lin), P_lin_z0)

        #Calculate growth factor for exact integral
        #Use (P(k=k_growth, z)/P(k=k_growth, z=0))^0.5
        #Actually just grab closet slice to k_growth
        growth_ind = (np.abs(k_lin-k_growth)).argmin()
        growth_vals = np.sqrt(np.divide(P_lin[:, growth_ind], P_lin[0, growth_ind], 
                    out=np.zeros_like(P_lin[:, growth_ind]), where=P_lin[:, growth_ind]!=0.))
        self.lin_growth_spline = interp.InterpolatedUnivariateSpline(chi_lin, growth_vals)

        #And finally a 2d spline of P_nl - P_lin
        #In the case that P_lin is not exactly separable, we're actually better
        #off using P_nl - D^2(chi)P_lin(0) here since that is what we're using in 
        #the exact calculation
        #We may also need to resample the linear P(k) on the nl grid :/
        assert np.allclose(z_lin, self.z_vals)
        P_lin_z0_resamp = interp.interp1d(np.log(k_lin), P_lin_z0, kind="cubic", bounds_error=False, fill_value=0.)(self.logk_vals)
        P_lin_from_growth = np.outer(growth_vals**2, P_lin_z0_resamp)
        P_sublin_vals = self.pk_vals - P_lin_from_growth
        self.sublin_spline = interp.RectBivariateSpline(self.chi_vals, self.logk_vals, P_sublin_vals)

class MatterPower3D(Power3D):
    section = "matter_power_nl"
    lin_section = "matter_power_lin"
    source_specific = False

class LinearMatterPower3D(Power3D):
    section = "matter_power"
    source_specific = False

class GalaxyPower3D(Power3D):
    section = "galaxy_power"
    lin_section = "galaxy_power_lin"
    source_specific = True

class IntrinsicPower3D(Power3D):
    section = "intrinsic_power"
    source_specific = True

class IntrinsicBBPower3D(Power3D):
    section = "intrinsic_power_bb"
    source_specific = True

class MatterGalaxyPower3D(Power3D):
    section = "matter_galaxy_power"
    source_specific = True

class MatterIntrinsicPower3D(Power3D):
    section = "matter_intrinsic_power"
    source_specific = True

class GalaxyIntrinsicPower3D(Power3D):
    section = "galaxy_intrinsic_power"
    source_specific = True

def lensing_prefactor(block):
    c_kms = 299792.4580
    omega_m = block[names.cosmological_parameters, "omega_m"]
    shear_scaling = 1.5 * (100.0*100.0)/(c_kms*c_kms) * omega_m
    return shear_scaling

class Spectrum(object):
    autocorrelation = False
    #These should make it more obvious if the values are not overwritten by subclasses
    power_3d_key = ("?", "?")
    kernel_types = ("?", "?")
    name = "?"
    prefactor_power = np.nan
    # the default is no magnification. If the two fields are magnification terms
    # that should pick up factors of 2 alpha_i - 1
    # then subclasses should include 1 and/or 2 in this.
    magnification_prefactors = {}

    def __init__(self, source, sample_a, sample_b, power_key, save_name=""):
        # caches of n(z), w(z), P(k,z), etc.
        self.source = source
        self.sample_a, self.sample_b = sample_a, sample_b
        self.power_key = power_key
        self.save_name = save_name
        if self.save_name:
            self.section_name = self.name + "_" + self.save_name
        else:
            self.section_name = self.name

    def nbins(self):
        na = self.source.kernels[self.sample_a].nbin
        nb = self.source.kernels[self.sample_b].nbin
        return na, nb

    def is_autocorrelation(self):
        """
        This is an autocorrelation if the basic type is an auto-correlation
        (e.g. shear-shear, position-position, but not shear position)
        and the two n(z) samples are the same.
        """
        return self.autocorrelation and (self.sample_a == self.sample_b)

    @classmethod
    def option_name(cls):
        """Convert the CamelCase name to hypen-separated.
        For example ShearShear becomes shear-shear
        """
        name = cls.__name__
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1-\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1-\2', s1).lower()

    def prefactor(self, block, bin1, bin2):
        if self.prefactor_power == 0:
            # This should be okay for the magnification terms
            # since none of those have prefactor_power==0
            return 1.0
        c_kms = 299792.4580
        omega_m = block[names.cosmological_parameters, "omega_m"]
        shear_scaling = 1.5 * (100.0 * 100.0) / (c_kms * c_kms) * omega_m
        f = shear_scaling ** self.prefactor_power
        if 1 in self.magnification_prefactors:
            f *= self.magnification_prefactor(block, bin1)
        if 2 in self.magnification_prefactors:
            f *= self.magnification_prefactor(block, bin2)
        return f

    # Some spectra include a magnification term for one or both bins.
    # In those cases an additional term from the galaxy luminosity
    # function is included in the prefactor.
    def magnification_prefactor(self, block, bin):
        # Need to move this so that it references a particular
        # galaxy sample not the generic galaxy_luminosity_function
        alpha = np.atleast_1d(np.array(block[names.galaxy_luminosity_function, "alpha_binned"]))
        return 2 * (alpha[bin] - 1)

    def compute_limber(self, block, ell, bin1, bin2, dchi=None, sig_over_dchi=100., 
        chimin=None, chimax=None):

        # Get the required power
        P_chi_logk_spline = self.get_power_spline(block, bin1, bin2)

        # Get the kernels
        K1 = (self.source.kernels[self.sample_a]).get_kernel_spline(self.kernel_types[0], bin1)
        K2 = (self.source.kernels[self.sample_b]).get_kernel_spline(self.kernel_types[1], bin2)

        #Need to choose a chimin, chimax and dchi for the integral.
        #By default
        if chimin is None:
            chimin = max( K1.xmin_clipped, K2.xmin_clipped )
        if chimax is None:
            chimax = min( K1.xmax_clipped, K2.xmax_clipped )
        if dchi is None:
            assert (sig_over_dchi is not None)
            dchi = min( K1.sigma/sig_over_dchi, K2.sigma/sig_over_dchi )

        c_ell, c_ell_err = limber_integral(ell, K1, K2, P_chi_logk_spline, chimin, 
            chimax, dchi)

        c_ell *= self.prefactor(block, bin1, bin2)
        return c_ell

    def compute_exact(self, block, ell, bin1, bin2, dlogchi=None, sig_over_dchi=10., chi_pad_lower=1., chi_pad_upper=1.,
        chimin=None, chimax=None, dchi_limber=None):
        #The 'exact' calculation is in two parts. Non-limber for the separable linear contribution, 
        #and then Limber for the non-linear part (P_nl-P_lin)

        # Get the kernels
        K1 = (self.source.kernels[self.sample_a]).get_kernel_spline(self.kernel_types[0], bin1)
        K2 = (self.source.kernels[self.sample_b]).get_kernel_spline(self.kernel_types[1], bin2)

        lin_z0_logk_spline, lin_growth_spline  = self.get_lin_power_growth(block, bin1, bin2)
        
        #Need to choose a chimin, chimax and dchi for the integral.
        #By default
        if chimin is None:
            chimin = max( K1.xmin_clipped, K2.xmin_clipped )
        if chimax is None:
            chimax = min( K1.xmax_clipped, K2.xmax_clipped )
        if dlogchi is None:
            sig = min( K1.sigma/sig_over_dchi, K2.sigma/sig_over_dchi )
            #We want a maximum dchi that is sig / sig_over_dchi where sig is the 
            #width of the narrower kernel.
            dchi = sig / sig_over_dchi
            dlogchi = get_dlogchi(dchi, chimax)

        #Exact calculation with linear P(k)
        c_ell = exact_integral(ell, K1, K2, lin_z0_logk_spline, lin_growth_spline,
            chimin, chimax, dlogchi, chi_pad_upper=chi_pad_upper,
            chi_pad_lower=chi_pad_lower)

        #Limber with P_nl-P_lin
        P_sublin_spline = self.get_power_sublin(block, bin1, bin2)
        if dchi_limber is None:
            assert (sig_over_dchi is not None)
            dchi_limber = min( K1.sigma/sig_over_dchi, K2.sigma/sig_over_dchi )

        c_ell_sublin,_ = limber_integral(ell, K1, K2, P_sublin_spline, chimin, 
            chimax, dchi_limber)

        c_ell += c_ell_sublin
        c_ell *= self.prefactor(block, bin1, bin2)

        return c_ell

    def compute(self, block, ell_limber, bin1, bin2, 
        dchi_limber=None, sig_over_dchi_limber=100., chimin=None, chimax=None, 
        ell_exact=None, exact_kwargs=None, cut_ell_limber=True):

        if ell_exact is not None:
            if cut_ell_limber:
                ell_limber = ell_limber[ell_limber>=ell_exact[-1]]
                
        #Do Limber calculation
        cl_limber = self.compute_limber(block, ell_limber, bin1, bin2,
            dchi=dchi_limber, sig_over_dchi=sig_over_dchi_limber, chimin=chimin, chimax=chimax)

        ell_out = ell_limber
        cl_out = cl_limber
        if ell_exact is not None:
            print("doing exact calculation for ells", ell_exact)
            #also do exact calculation
            cl_exact = self.compute_exact(block, ell_exact, bin1, bin2, **exact_kwargs)        
            use_limber = ell_limber > ell_exact[-1]
            ell_out = np.concatenate((ell_exact, ell_limber[use_limber]))
            cl_out = np.concatenate((cl_exact, cl_limber[use_limber]))

        return ell_out, cl_out

    def get_power_spline(self, block, bin1, bin2):
        return (self.source.power[self.power_key]).chi_logk_spline

    def get_power_sublin(self, block, bin1, bin2):
        return (self.source.power[self.power_key]).sublin_spline

    def get_lin_power_growth(self, block, bin1, bin2):
        p = self.source.power[self.power_key]
        return p.lin_z0_logk_spline, p.lin_growth_spline

    def clean_power(self, P):
        # This gets done later for the base class
        return 0

    def prep_spectrum(self, *args, **kwargs):
        # no prep required for base class
        return 0

# This is pretty cool.
# You can make an enumeration class which
# contains a list of possible options for something.
# But the options can each be anything - full python objects, and in this
# case they are classes of spectrum. So we can easily look these up by name,
# loop through them, etc.
class SpectrumType(Enum):
    class ShearShear(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("W", "W")
        autocorrelation = True
        name = names.shear_cl
        prefactor_power = 2

    class ShearIntrinsic(Spectrum):
        power_3d_type = MatterIntrinsicPower3D
        kernel_types = ("W", "N")
        autocorrelation = False
        name = names.shear_cl_gi
        prefactor_power = 1

    class IntrinsicIntrinsic(Spectrum):
        power_3d_type = IntrinsicPower3D
        kernel_types = ("N", "N")
        autocorrelation = True
        name = names.shear_cl_ii
        prefactor_power = 0

    class IntrinsicbIntrinsicb(Spectrum):
        power_3d_type = IntrinsicBBPower3D
        kernel_types = ("N", "N")
        autocorrelation = True
        name = "shear_cl_bb"
        prefactor_power = 0

    #class DensityDensity(Spectrum):
    #    power_3d_type = MatterPower3D
    #    kernel_types = ("N", "N")
    #    autocorrelation = True
    #    name = "galaxy_cl"
    #    prefactor_power = 0

    class PositionPosition(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("N", "N")
        autocorrelation = True
        name = "galaxy_cl"
        prefactor_power = 0

    class MagnificationDensity(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("W", "N")
        autocorrelation = False
        name = "magnification_density_cl"
        prefactor_power = 1
        magnification_prefactors = (1,)

    class MagnificationMagnification(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("W", "W")
        autocorrelation = True
        name = "magnification_cl"
        prefactor_power = 2
        magnification_prefactors = (1, 2)

    class DensityShear(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("N", "W")
        autocorrelation = False
        name = "galaxy_shear_cl"
        prefactor_power = 1

    class DensityIntrinsic(Spectrum):
        power_3d_type = MatterIntrinsicPower3D
        kernel_types = ("N", "N")
        autocorrelation = False
        name = "galaxy_intrinsic_cl"
        prefactor_power = 0

    class MagnificationIntrinsic(Spectrum):
        power_3d_type = MatterIntrinsicPower3D
        kernel_types = ("W", "N")
        autocorrelation = False
        name = "magnification_intrinsic_cl"
        prefactor_power = 1
        magnification_prefactors = (1,)

    class MagnificationShear(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("W", "W")
        autocorrelation = False
        name = "magnification_shear_cl"
        prefactor_power = 2
        magnification_prefactors = (1,)

    class ShearCmbkappa(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("W", "K")
        autocorrelation = False
        name = "shear_cmbkappa_cl"
        prefactor_power = 2

    class CmbkappaCmbkappa(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("K", "K")
        autocorrelation = True
        name = "cmbkappa_cl"
        prefactor_power = 2

    class IntrinsicCmbkappa(Spectrum):
        power_3d_type = MatterIntrinsicPower3D
        kernel_types = ("N", "K")
        autocorrelation = False
        name = "intrinsic_cmbkappa_cl"
        prefactor_power = 1

    class DensityCmbkappa(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("N", "K")
        autocorrelation = False
        name = "galaxy_cmbkappa_cl"
        prefactor_power = 1

    class FastShearShearIA(Spectrum):
        """
        Variant method of Shear+IA calculation that
        does the integral all at once including the shear 
        components.  Only works for scale-independent IA/
        """
        power_3d_type = MatterPower3D
        kernel_types = ("F", "F")
        autocorrelation = True
        name = names.shear_cl
        prefactor_power = 2

    class FastDensityShearIA(Spectrum):
        power_3d_type = MatterPower3D
        kernel_types = ("N", "F")
        autocorrelation = False
        name = "galaxy_shear_cl"
        prefactor_power = 1

class SpectrumCalculator(object):
    # It is useful to put this here so we can subclass to add new spectrum
    # types, for example ones done with modified gravity changes.
    spectrumType = SpectrumType

    def __init__(self, options):
        # General options
        self.verbose = options.get_bool(option_section, "verbose", False)
        self.fatal_errors = options.get_bool(option_section, "fatal_errors", False)
        self.get_kernel_peaks = options.get_bool(option_section, "get_kernel_peaks", False)
        self.save_kernel_zmax = options.get_double(option_section, "save_kernel_zmax", -1.0)
        
        self.limber_ell_start = options.get_int(option_section, "limber_ell_start", 300)
        do_exact_string = options.get_string(option_section, "do_exact", "")
        if do_exact_string=="":
            self.do_exact_option_names=[]
        else:
            self.do_exact_option_names = (do_exact_string.strip()).split(" ")
        self.clip_chi_kernels = options.get_double(option_section, "clip_chi_kernels", 1.e-6)
        
        #accuracy settings
        self.sig_over_dchi = options.get_double(option_section, "sig_over_dchi", 50.)
        self.shear_kernel_nchi = options.get_int(option_section, "shear_kernel_nchi", 2000)

        self.limber_transition_end = options.get_double(option_section,
            "limber_transition_end", -1.)
        self.smooth_limber_transition = options.get_bool(option_section, 
            "smooth_limber_transition", True)
        if self.limber_transition_end<0:
            self.limber_transition_end = 1.2*self.limber_ell_start
        try:
            assert self.limber_transition_end > self.limber_ell_start
        except AssertionError as e:
            print("limber_transition_end must be larger than limber_ell_start")
            raise(e)

        # And the req ell ranges.
        # We use log-spaced output
        # These are the ells for the Limber calculation, which don't need
        # to be integers.
        ell_min = options.get_double(option_section, "ell_min")
        ell_max = options.get_double(option_section, "ell_max")
        n_ell = options.get_int(option_section, "n_ell")
        self.ell_limber = np.logspace(np.log10(ell_min), np.log10(ell_max), n_ell)

        #Sort out ells for exact calculation
        #We set a limber_ell_start and n_ell_exact in the options
        #Do integer and (approximately) log-spaced ells between 0 and 
        #limber_ell_start, and always to 1 also. 
        #accuracy settings for exact integral
        self.n_ell_exact = options.get_int(option_section, "n_ell_exact", 50)
        self.dlogchi = options.get_int(option_section, "dlogchi", -1)
        if self.dlogchi < 0:
            self.dlogchi = None
        self.chi_pad_upper = options.get_double(option_section, "chi_pad_upper", 2.)
        self.chi_pad_lower = options.get_double(option_section, "chi_pad_lower", 2.)
        self.save_limber = options.get_bool(option_section, "save_limber", True)

        if len(self.do_exact_option_names)>0:
            sig_over_dchi_exact = options.get_double(option_section, "sig_over_dchi_exact", 10.)
            self.exact_ell_max = self.limber_ell_start
            #Make these ~log-spaced integers. Always do 0 and 1. Remove any repeated 
            #entries. Note then that the number may not be exactly self.n_ell_exact,
            #so update that afterwards.
            self.ell_exact = np.ceil(np.linspace(1., self.exact_ell_max, self.n_ell_exact-1))
            self.ell_exact = np.concatenate((np.array([0]), self.ell_exact))
            _, unique_inds = np.unique(self.ell_exact, return_index=True)
            self.ell_exact = self.ell_exact[unique_inds]
            self.n_ell_exact = len(self.ell_exact)
            self.exact_kwargs = { "sig_over_dchi": sig_over_dchi_exact,
                               "dlogchi": self.dlogchi,
                               "chi_pad_lower": self.chi_pad_lower,
                               "chi_pad_upper": self.chi_pad_upper}

            #also slip limber_ell_start into the ell values for the limber calculation
            #as it's nice to have both methods at the transition.
            #low = self.ell_limber<self.limber_ell_start
            #self.ell_limber = np.concatenate((self.ell_limber[low], np.array([self.limber_ell_start]), self.ell_limber[~low]))
        else:
            self.exact_kwargs = None

        # Check which spectra we are requested to calculate
        self.parse_requested_spectra(options)
        print("Will project these spectra into 2D:")
        for spectrum in self.req_spectra:
            print("    - ", spectrum.section_name)
            if spectrum.section_name in self.do_exact_section_names:
                print("Doing exact calculation up to ell=%d"%(self.ell_exact.max()))


        self.kernels = {}
        self.power = {}
        self.outputs = {}

    def parse_requested_spectra(self, options):
        # Get the list of spectra that we want to compute.
        # List of Spectrum objects that we need to compute
        self.req_spectra = []

        # List of keys which determine which kernels are required. These
        # are tuples of (kernel_type, sample_name), where kernel_type is 
        # "N" or "W" (for number density and lensing respectively), and
        # sample name determines the n(z) section i.e. nz_<sample_name>.
        self.req_kernel_keys = set()

        #List of options that determines which 3d power spectra
        #are required for the spectra. These are tuples of 
        #(PowerType, suffix, do_exact) where
        #- PowerType is a Power3d or child object
        #- suffix is a suffix for the section name.
        #- do_exact is a bool, whether or not we're doing
        #the exact calclation and thus need to load in extra stuff
        self.req_power_options = set()
        self.do_exact_section_names = [] 

        any_spectra_option_found = False
        for spectrum in self.spectrumType:

            spectrum = spectrum.value
            #By default we just do the shear-shear spectrum.
            #everything else is not done by default
            option_name = spectrum.option_name()
            option_name = option_name.replace("density", "position")
            try:
                value = options[option_section, option_name]
            # if value is not set at all, skip
            except BlockError:
                continue

            # If we find any of the options set then record that
            any_spectra_option_found = True

            # There are various ways a user can describe the spectra:
            # string of form  euclid-lsst
            #   (one pair of n(z) samples to correlate,
            #   output in the default section)
            #   euclid-lsst:cross  euclid-euclid:auto
            #   (one or more pairs, output in the named section)

            # Otherwise it must be a string - enforce this.
            if not (isinstance(value, str) or isinstance(value, unicode)):
                raise ValueError("Unknown form of value for option {} in project_2d: {}".format(option_name, value))

            value = value.strip()
            if not value:
                raise ValueError("Empty value for option {} in project_2d.".format(option_name))

            # now we are looking for things of the form
            # shear-shear = euclid-ska[:name]
            # where we would now search for nz_euclid and nz_ska
            values = value.split()
            for value in values:
                try:
                    sample_name_a, sample_name_b = value.split('-', 1)
                    # Optionally we can also name the spectrum, for example
                    # shear-shear = ska-ska:radio
                    # in which case the result will be saved into shear_cl_radio
                    # instead of just shear_cl.
                    # This will be necessary in the case that we run multiple spectra,
                    # e.g.
                    # #shear-shear = euclid-ska:cross  euclid-euclid:optical  ska-ska:radio

                    # would be needed to avoid clashes. 
                    # Can also allow
                    # #intrinsic-intrinsic = des_source-des_source:des_power:des_cl kids_source-kids_source:kids_power:kids_cl
                    # to use the suffix XXX or YYY on the IA and galaxy density 3D power spectrum inputs
                    if ":" in sample_name_b:
                        sample_name_b, save_name=sample_name_b.split(":",1)
                        if ":" in save_name:
                            power_suffix, save_name = save_name.split(':',1)
                            power_suffix = "_"+power_suffix
                        else:
                            power_suffix = ""
                    else:
                        save_name = ""
                        power_suffix = ""

                    sample_name_a = sample_name_a.strip()
                    sample_name_b = sample_name_b.strip()
                    kernel_key_a = (spectrum.kernel_types[0], sample_name_a)
                    kernel_key_b = (spectrum.kernel_types[0], sample_name_a)
                    self.req_kernel_keys.add(kernel_key_a)
                    self.req_kernel_keys.add(kernel_key_b)

                    #power_key is the power_3d class and suffix
                    power_key = (spectrum.power_3d_type, power_suffix)

                    #The self in the line below is not a mistake - the source objects
                    #for the spectrum class is the SpectrumCalculator itself
                    s = spectrum(self, sample_name_a, sample_name_b, power_key, save_name)
                    self.req_spectra.append(s)

                    if option_name in self.do_exact_option_names:
                        print("doing exact for option_name %s"%option_name)
                        power_options = power_key + (True,)
                        #It may be that the same power_key, but with do_exact=False is already 
                        #in self.req_power_keys. If this is the case remove it. We don't need it.
                        #It's dead to us.
                        try:
                            self.req_power_options.remove( power_key + (False,) )
                        except KeyError:
                            pass
                        self.do_exact_section_names.append(s.section_name)
                    else:
                        power_options = power_key + (False,)
                    self.req_power_options.add(power_options)
                    
                    print("Calculating Limber: Kernel 1 = {}, Kernel 2 = {}, P_3D = {},{} --> Output: {}".format(
                        str(kernel_key_a), str(kernel_key_b), str(power_key[0]), 
                        str(power_key[1]), s.section_name))
                except:
                    raise
                    raise ValueError("""To specify a P(k)->C_ell projection with one or more sets of two different n(z) 
                        samples use the form shear-shear=sample1-sample2 sample3-sample4 ....  Otherwise just use 
                        shear-shear=T to use the standard form.""")

        #If no other spectra are specified, just do the shear-shear spectrum.
        if not any_spectra_option_found:
            print()
            print("No spectra requested in the parameter file.")  
            print("I will go along with this and just do nothing,")
            print("but if you get a crash later this is probably why.")
            print()

    def load_distance_splines(self, block):
        # Extract some useful distance splines
        # have to copy these to get into C ordering (because we reverse them)
        z_distance = block[names.distances, 'z']
        a_distance = block[names.distances, 'a']
        chi_distance = block[names.distances, 'd_m']
        if z_distance[1] < z_distance[0]:
            z_distance = z_distance[::-1].copy()
            a_distance = a_distance[::-1].copy()
            chi_distance = chi_distance[::-1].copy()

        h0 = block[names.cosmological_parameters, "h0"]

        # convert Mpc to Mpc/h
        chi_distance *= h0

        if block.has_value(names.distances, 'CHISTAR'):
            self.chi_star = block[names.distances, 'CHISTAR'] * h0
        else:
            self.chi_star = None
        self.chi_max = chi_distance.max()
        self.a_of_chi = GSLSpline(chi_distance, a_distance)
        #self.chi_of_z = GSLSpline(z_distance, chi_distance)
        self.a_of_chi = interp.InterpolatedUnivariateSpline(chi_distance, a_distance)
        self.chi_of_z = interp.InterpolatedUnivariateSpline(z_distance, chi_distance)
        self.dchidz = self.chi_of_z.derivative()
        self.chi_distance = chi_distance

    def load_kernels(self, block):
        # During the setup we already decided what kernels (W(z) or N(z) splines)
        # we needed for the spectra we want to do. Load them now and add to the 
        # self.kernels dictionary.
        for key in self.req_kernel_keys:
            kernel_type, sample_name = key
            if sample_name not in self.kernels:
                section_name = "nz_"+sample_name
                self.kernels[sample_name] = TomoNzKernel.from_block(block, section_name, norm=True)
            if kernel_type == "N":
                print("setting up N(chi) kernel for sample %s"%sample_name)
                self.kernels[sample_name].set_nofchi_splines(self.chi_of_z, self.dchidz, 
                    clip=self.clip_chi_kernels)
            elif kernel_type == "W":
                print("setting up W(chi) kernel for sample %s"%sample_name)
                self.kernels[sample_name].set_wofchi_splines(self.chi_of_z, self.dchidz, self.a_of_chi, 
                    clip=self.clip_chi_kernels, nchi=self.shear_kernel_nchi) 
            else:
                raise ValueError("Invalid kernel type: %s. Should be 'N' or 'W'"%kernel_type)

    def load_power(self, block):
        #Loop through keys in self.req_power_keys, initializing the Power3d
        #instances and adding to the self.power dictionary.
        for power_options in self.req_power_options:
            powertype, suffix, do_exact = power_options
            power = powertype(suffix)
            power.load_from_block(block, self.chi_of_z)
            power.set_chi_logk_spline()
            if do_exact:
                print("setting nonlimber splines for power", powertype, suffix)
                power.set_nonlimber_splines(block, self.chi_of_z)
            power_key = (powertype, suffix)
            self.power[power_key] = power
            print power_options, power.lin_growth_spline

    def compute_spectra(self, block, spectrum):

        print("doing spectrum:", spectrum.section_name)

        #Save some info about the spectrum
        block[spectrum.section_name, "save_name"] = spectrum.save_name
        block[spectrum.section_name, "sample_a"] = spectrum.sample_a
        block[spectrum.section_name, "sample_b"] = spectrum.sample_b
        sep_name = "ell"
        block[spectrum.section_name, "sep_name"] = sep_name
        na, nb = spectrum.nbins()
        if spectrum.is_autocorrelation():
            block[spectrum.section_name, 'nbin'] = na
        block[spectrum.section_name, 'nbin_a'] = na
        block[spectrum.section_name, 'nbin_b'] = nb
        #Save is_auto 
        block[spectrum.section_name, 'is_auto'] = spectrum.is_autocorrelation()

        spectrum.prep_spectrum( block, self.chi_of_z, na, nbin2=nb)

        print("computing spectrum %s for samples %s, %s"%(spectrum.__class__, spectrum.sample_a, spectrum.sample_b))

        for i in range(na):
            #for auto-correlations C_ij = C_ji so we calculate only one of them,
            #but save both orderings to the block to account for different ordering
            #conventions.
            #for cross-correlations we must do both
            jmax = i+1 if spectrum.is_autocorrelation() else nb
            for j in range(jmax):

                if spectrum.section_name in self.do_exact_section_names:
                    ell, c_ell = spectrum.compute(block, self.ell_limber, i+1, j+1, 
                        sig_over_dchi_limber=self.sig_over_dchi, ell_exact=self.ell_exact,
                        exact_kwargs=self.exact_kwargs)
                else:
                    ell, c_ell = spectrum.compute(block, self.ell_limber, i+1, j+1, 
                        sig_over_dchi_limber=self.sig_over_dchi)

                #c_ell_limber = spectrum.compute( block, self.ell, i, j, 
                #                          relative_tolerance=self.relative_tolerance, 
                #                          absolute_tolerance=self.absolute_tolerance )
                #if spectrum.option_name() in self.do_exact_option_names:
                #    if self.save_limber:
                #        block[spectrum_name, "ell_limber"] = self.ell
                #        block[spectrum_name, 'bin_limber_{}_{}'.format(i+1,j+1)] = c_ell_limber
                #    c_ell_exact = spectrum.compute_exact(block, self.ell_exact, i, j, 
                #        self.chi_of_z_pyspline, sig_over_dchi=self.sig_over_dchi, 
                #        dlogchi=self.dlogchi, chi_pad_lower=self.chi_pad_lower, 
                #        chi_pad_upper=self.chi_pad_upper)
#
                #    use_limber_inds = np.where(self.ell>self.ell_exact[-1])[0]
                #    ell_out = np.concatenate((self.ell_exact, self.ell[use_limber_inds]))
                #    c_ell = np.concatenate((c_ell_exact, c_ell_limber[use_limber_inds]))
                #    if self.smooth_limber_transition and len(use_limber_inds)>0:
                #        print("""applying smoothing transition in ell range [%f,%f]
                #         between exact and Limber predictions"""%(self.ell_exact[-1], self.limber_transition_end))
                #        #find transition start and end indices
                #        transition_start_ind = len(self.ell_exact)-1
                #        transition_end_ind = np.argmin(np.abs(ell_out-self.limber_transition_end))
                #        ell_transition_end = ell_out[transition_end_ind]
                #        transition_inds = np.arange(transition_start_ind, transition_end_ind+1)
                #        transition_ells = ell_out[transition_inds]
                #        cl_fracdiff_at_transition = c_ell_exact[-1]/c_ell_limber[use_limber_inds[0]-1] - 1.
                #        L_transition = transition_ells[-1]-transition_ells[0] 
                #        x = (ell_transition_end-transition_ells)/L_transition
                #        sin_filter = 1. - 0.5*(np.sin(np.pi*(x+0.5))+1)
                #        transition_filter = np.ones_like(c_ell)
                #        apply_filter_inds = transition_inds[1:]
                #        transition_filter[apply_filter_inds] = 1 + cl_fracdiff_at_transition*sin_filter[1:]
                #        c_ell *= transition_filter
                #else:
                #    ell_out = self.ell
                #    c_ell = c_ell_limber


                block[spectrum.section_name, sep_name] = ell
                block[spectrum.section_name, 'bin_{}_{}'.format(i+1,j+1)] = c_ell

    def clean(self):
        # need to manually delete power spectra we have loaded
        self.power.clear()
        #self.power_lin_z0.clear()
        #self.growth_lin.clear()
        #self.power_sublin.clear()

        # spectra know how to delete themselves, in gsl_wrappers.py
        self.kernels.clear()
        self.outputs.clear()

    def execute(self, block):
        try:
            self.load_distance_splines(block)
            # self.load_matter_power(block, self.chi_of_z)
            try:
                t0 = timer()
                self.load_kernels(block)
                t1 = timer()
                print("time to set up kernels: %s"%(str(timedelta(seconds=(t1-t0)))))
                #self.load_py_kernels(block, clip=self.clip_nzs)
            except NullSplineError:
                sys.stderr.write("Failed to load one of the kernels (n(z) or W(z)) needed to compute 2D spectra\n")
                sys.stderr.write("Often this is because you are in a weird part of parameter space, but if it is \n")
                sys.stderr.write(
                    "consistent then you may wish to look into it in more detail. Set fata_errors=T to do so.\n")
                if self.fatal_errors:
                    raise
                return 1
            if self.save_kernel_zmax > 0:
                self.save_kernels(block, self.save_kernel_zmax)
            t0 = timer()
            self.load_power(block)
            t1 = timer()
            print("time to load power: %s"%(str(timedelta(seconds=(t1-t0)))))
            #self.load_power_lin(block)
            for spectrum in self.req_spectra:
                t0 = timer()
                if self.verbose:
                    print("Computing spectrum: {} -> {}".format(spectrum.__class__.__name__, spectrum.section_name))
                self.compute_spectra(block, spectrum)
                t1 = timer()
                print("time to compute spectrum %s: %s"%(spectrum.section_name, str(timedelta(seconds=(t1-t0)))) )
        finally:
            self.clean()
        return 0


def setup(options):
    return SpectrumCalculator(options)


def execute(block, config):
    return config.execute(block)

