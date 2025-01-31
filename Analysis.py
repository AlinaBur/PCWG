import pandas as pd
import numpy as np
import scipy as sp
import hashlib

import os
import datetime
import math
import configuration
import dataset
from dataset import  DeviationMatrix
import binning
import turbine
import rews
import reporting


def chckMake(path):
    """Make a folder if it doesn't exist"""
    if not os.path.exists(path):
        os.mkdir(path)

def hash_file_contents(file_path):
    with open(file_path, 'r') as f:
        uid = hashlib.sha1(''.join(f.read().split())).hexdigest()
    return uid

class NullStatus:
    def __nonzero__(self):
        return False

    def addMessage(self, message):
        pass


class DensityCorrectionCalculator:

    def __init__(self, referenceDensity, windSpeedColumn, densityColumn):

        self.referenceDensity = referenceDensity
        self.windSpeedColumn = windSpeedColumn
        self.densityColumn = densityColumn

    def densityCorrectedHubWindSpeed(self, row):

        return row[self.windSpeedColumn] * (row[self.densityColumn] / self.referenceDensity) ** (1.0 / 3.0)


class PowerCalculator:

    def __init__(self, powerCurve, windSpeedColumn):

        self.powerCurve = powerCurve
        self.windSpeedColumn = windSpeedColumn

    def power(self, row):
        return self.powerCurve.power(row[self.windSpeedColumn])

class TurbulencePowerCalculator:

    def __init__(self, powerCurve, ratedPower, windSpeedColumn, turbulenceColumn):

        self.powerCurve = powerCurve
        self.ratedPower = ratedPower
        self.windSpeedColumn = windSpeedColumn
        self.turbulenceColumn = turbulenceColumn

    def power(self, row):
        return self.powerCurve.power(row[self.windSpeedColumn], row[self.turbulenceColumn])


class PowerDeviationMatrixPowerCalculator:

    def __init__(self, powerCurve, powerDeviationMatrix, windSpeedColumn, parameterColumns):

        self.powerCurve = powerCurve
        self.powerDeviationMatrix = powerDeviationMatrix
        self.windSpeedColumn = windSpeedColumn
        self.parameterColumns = parameterColumns

    def power(self, row):

        parameters = {}

        for dimension in self.powerDeviationMatrix.dimensions:
            column = self.parameterColumns[dimension.parameter]
            value = row[column]
            parameters[dimension.parameter] = value
            #print dimension.parameter, value

        deviation = self.powerDeviationMatrix[parameters]
        power = self.powerCurve.power(row[self.windSpeedColumn])

        #print power, deviation
        #raise Exception("Stop")
    
        return self.powerCurve.power(row[self.windSpeedColumn]) * (1.0 + deviation)

class Analysis:

    def __init__(self, config, status = NullStatus(), auto_activate_corrections = False):

        self.config = config
        self.nameColumn = "Dataset Name"
        self.inputHubWindSpeed = "Input Hub Wind Speed"
        self.densityCorrectedHubWindSpeed = "Density Corrected Hub Wind Speed"
        self.rotorEquivalentWindSpeed = "Rotor Equivalent Wind Speed"
        self.basePower = "Simulated Reference TI Power"
        self.hubPower = "Hub Power"
        self.rewsPower = "REWS Power"
        self.powerDeviationMatrixPower = "Power Deviation Matrix Power"
        self.turbulencePower = "Simulated TI Corrected Power"
        self.combinedPower = "Combined Power"
        self.windSpeedBin = "Wind Speed Bin"
        self.turbulenceBin = "Turbulence Bin"
        self.powerDeviation = "Power Deviation"
        self.dataCount = "Data Count"
        self.powerStandDev = "Power Standard Deviation"
        self.windDirection = "Wind Direction"
        self.powerCoeff = "Power Coefficient"
        self.inputHubWindSpeedSource = 'Undefined'
        self.measuredTurbulencePower = 'Measured TI Corrected Power'
        self.measuredTurbPowerCurveInterp = 'Measured TI Corrected Power Curve Interp'
        self.measuredPowerCurveInterp = 'All Measured Power Curve Interp'
        self.inflowAngle = 'Inflow Angle'
        self.relativePath = configuration.RelativePath(config.path)
        self.status = status

        self.calibrations = []
        
        self.rotorGeometry = turbine.RotorGeometry(config.diameter, config.hubHeight)

        self.status.addMessage("Loading dataset...")
        self.loadData(config, self.rotorGeometry)

        if auto_activate_corrections:            
            self.auto_activate_corrections()
        self.densityCorrectionActive = config.densityCorrectionActive
        self.rewsActive = config.rewsActive
        self.turbRenormActive = config.turbRenormActive
        self.powerDeviationMatrixActive = config.powerDeviationMatrixActive
        
        self.uniqueAnalysisId = hash_file_contents(self.config.path)
        self.status.addMessage("Unique Analysis ID is: %s" % self.uniqueAnalysisId)
        self.status.addMessage("Calculating (please wait)...")
        if len(self.datasetConfigs) > 0:
            self.datasetUniqueIds = self.generate_unique_dset_ids()

        if self.powerDeviationMatrixActive:
            self.status.addMessage("Loading power deviation matrix...")
            self.specifiedPowerDeviationMatrix = configuration.PowerDeviationMatrixConfiguration(self.relativePath.convertToAbsolutePath(config.specifiedPowerDeviationMatrix))

        self.powerCurveMinimumCount = config.powerCurveMinimumCount
        self.powerCurvePaddingMode = config.powerCurvePaddingMode
        self.ratedPower = config.ratedPower

        self.baseLineMode = config.baseLineMode
        self.interpolationMode = config.interpolationMode
        self.filterMode = config.filterMode
        self.powerCurveMode = config.powerCurveMode

        self.defineInnerRange(config)

        self.status.addMessage("Baseline Mode: %s" % self.baseLineMode)
        self.status.addMessage("Interpolation Mode: %s" % self.interpolationMode)
        self.status.addMessage("Filter Mode: %s" % self.filterMode)
        self.status.addMessage("Power Curve Mode: %s" % self.powerCurveMode)

        self.windSpeedBins = binning.Bins(config.powerCurveFirstBin, config.powerCurveBinSize, config.powerCurveLastBin)

        first_turb_bin = 0.01
        turb_bin_width = 0.02
        last_turb_bin = 0.25

        self.powerCurveSensitivityResults = {}
        self.powerCurveSensitivityVariationMetrics = pd.DataFrame(columns = ['Power Curve Variation Metric'])

        self.turbulenceBins = binning.Bins(first_turb_bin, turb_bin_width, last_turb_bin)
        self.aggregations = binning.Aggregations(self.powerCurveMinimumCount)
        
        if config.specifiedPowerCurve != '':

            powerCurveConfig = configuration.PowerCurveConfiguration(self.relativePath.convertToAbsolutePath(config.specifiedPowerCurve))
            
            self.specifiedPowerCurve = turbine.PowerCurve(powerCurveConfig.powerCurveLevels, powerCurveConfig.powerCurveDensity, \
                                                          self.rotorGeometry, "Specified Power", "Specified Turbulence", \
                                                          turbulenceRenormalisation = self.turbRenormActive, name = 'Specified', interpolationMode = self.interpolationMode)

            self.referenceDensity = self.specifiedPowerCurve.referenceDensity
            
        else:
             
            self.specifiedPowerCurve = None
            self.referenceDensity = 1.225 #todo consider adding UI setting for this
        
        if self.densityCorrectionActive:
            if self.hasDensity:
                self.status.addMessage("Performing Density Correction")
                self.status.addMessage("Mean measured density is %.4f kg/m^3" % self.dataFrame[self.hubDensity].mean())
                self.status.addMessage("Correcting to reference density of %.4f kg/m^3" % self.referenceDensity)
                self.dataFrame[self.densityCorrectedHubWindSpeed] = self.dataFrame.apply(DensityCorrectionCalculator(self.referenceDensity, self.hubWindSpeed, self.hubDensity).densityCorrectedHubWindSpeed, axis=1)
                self.dataFrame[self.inputHubWindSpeed] = self.dataFrame[self.densityCorrectedHubWindSpeed]
                self.inputHubWindSpeedSource = self.densityCorrectedHubWindSpeed
            else:
                raise Exception("Density data column not specified.")
        else:
            self.dataFrame[self.inputHubWindSpeed] = self.dataFrame[self.hubWindSpeed]
            self.inputHubWindSpeedSource = self.hubWindSpeed

        self.dataFrame[self.windSpeedBin] = self.dataFrame[self.inputHubWindSpeed].map(self.windSpeedBins.binCenter)
        self.dataFrame[self.turbulenceBin] = self.dataFrame[self.hubTurbulence].map(self.turbulenceBins.binCenter)

        self.applyRemainingFilters() #To do: record rows which are removed by each filter independently, as opposed to sequentially.

        if self.hasDensity:
            if self.densityCorrectionActive:
                self.dataFrame[self.powerCoeff] = self.calculateCp()
            self.meanMeasuredSiteDensity = self.dataFrame[self.hubDensity].dropna().mean()            
               
        if self.hasActualPower:

            self.status.addMessage("Calculating actual power curves...")

            self.allMeasuredPowerCurve = self.calculateMeasuredPowerCurve(0, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.actualPower, 'All Measured')
            
            self.dayTimePowerCurve = self.calculateMeasuredPowerCurve(11, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.actualPower, 'Day Time')
            self.nightTimePowerCurve = self.calculateMeasuredPowerCurve(12, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.actualPower, 'Night Time')

            self.innerTurbulenceMeasuredPowerCurve = self.calculateMeasuredPowerCurve(2, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.actualPower, 'Inner Turbulence')
            self.outerTurbulenceMeasuredPowerCurve = self.calculateMeasuredPowerCurve(2, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.actualPower, 'Outer Turbulence')

            if self.hasShear:
                self.innerMeasuredPowerCurve = self.calculateMeasuredPowerCurve(1, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.actualPower, 'Inner Range', required = (self.powerCurveMode == 'InnerMeasured'))
                self.outerMeasuredPowerCurve = self.calculateMeasuredPowerCurve(4, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.actualPower, 'Outer Range', required = (self.powerCurveMode == 'OuterMeasured'))

            self.status.addMessage("Actual Power Curves Complete.")

        self.powerCurve = self.selectPowerCurve(self.powerCurveMode)

        self.calculateBase()
        self.calculateHub()

        #Normalisation Parameters
        if self.turbRenormActive:
            self.normalisingRatedPower = self.powerCurve.zeroTurbulencePowerCurve.initialZeroTurbulencePowerCurve.selectedStats.ratedPower
            self.normalisingRatedWindSpeed = self.powerCurve.zeroTurbulencePowerCurve.initialZeroTurbulencePowerCurve.ratedWindSpeed
            self.normalisingCutInWindSpeed = self.powerCurve.zeroTurbulencePowerCurve.initialZeroTurbulencePowerCurve.selectedStats.cutInWindSpeed

            print "normalisation"
            print self.normalisingRatedWindSpeed
            print self.normalisingCutInWindSpeed
            
            self.normalisedWS = 'Normalised WS'
            self.dataFrame[self.normalisedWS] = (self.dataFrame[self.inputHubWindSpeed] - self.normalisingCutInWindSpeed) / (self.normalisingRatedWindSpeed - self.normalisingCutInWindSpeed)

        if self.hasActualPower:
            self.normalisedPower = 'Normalised Power'
            self.dataFrame[self.normalisedPower] = self.dataFrame[self.actualPower] / self.config.ratedPower

        if config.rewsActive:
            self.status.addMessage("Calculating REWS Correction...")
            self.calculateREWS()
            self.status.addMessage("REWS Correction Complete.")

            self.rewsMatrix = self.calculateREWSMatrix(0)
            if self.hasShear: self.rewsMatrixInnerShear = self.calculateREWSMatrix(3)
            if self.hasShear: self.rewsMatrixOuterShear = self.calculateREWSMatrix(6)

        if config.turbRenormActive:
            self.status.addMessage("Calculating Turbulence Correction...")
            self.calculateTurbRenorm()
            self.status.addMessage("Turbulence Correction Complete.")
            if self.hasActualPower:
                self.allMeasuredTurbCorrectedPowerCurve = self.calculateMeasuredPowerCurve(0, config.cutInWindSpeed, config.cutOutWindSpeed, config.ratedPower, self.measuredTurbulencePower, 'Turbulence Corrected')

        if config.turbRenormActive and config.rewsActive:
            self.status.addMessage("Calculating Combined (REWS + Turbulence) Correction...")
            self.calculationCombined()

        if config.powerDeviationMatrixActive:
            self.status.addMessage("Calculating Power Deviation Matrix Correction...")
            self.calculatePowerDeviationMatrixCorrection()
            self.status.addMessage("Power Deviation Matrix Correction Complete.")

        if self.hasActualPower:

            self.status.addMessage("Calculating power deviation matrices...")

            allFilterMode = 0
            innerShearFilterMode = 3

            self.hubPowerDeviations = self.calculatePowerDeviationMatrix(self.hubPower, allFilterMode)
            if self.hasShear: self.hubPowerDeviationsInnerShear = self.calculatePowerDeviationMatrix(self.hubPower, innerShearFilterMode)

            if config.rewsActive:
                self.rewsPowerDeviations = self.calculatePowerDeviationMatrix(self.rewsPower, allFilterMode)
                if self.hasShear: self.rewsPowerDeviationsInnerShear = self.calculatePowerDeviationMatrix(self.rewsPower, innerShearFilterMode)

            if config.turbRenormActive:
                self.turbPowerDeviations = self.calculatePowerDeviationMatrix(self.turbulencePower, allFilterMode)
                if self.hasShear: self.turbPowerDeviationsInnerShear = self.calculatePowerDeviationMatrix(self.turbulencePower, innerShearFilterMode)

            if config.turbRenormActive and config.rewsActive:
                self.combPowerDeviations = self.calculatePowerDeviationMatrix(self.combinedPower, allFilterMode)
                if self.hasShear: self.combPowerDeviationsInnerShear = self.calculatePowerDeviationMatrix(self.combinedPower, innerShearFilterMode)

            if config.powerDeviationMatrixActive:
                self.powerDeviationMatrixDeviations = self.calculatePowerDeviationMatrix(self.powerDeviationMatrixPower, allFilterMode)

            self.status.addMessage("Power Curve Deviation Matrices Complete.")
        self.hours = len(self.dataFrame.index)*1.0 / 6.0
        if self.config.nominalWindSpeedDistribution is not None:
            self.status.addMessage("Attempting AEP Calculation...")
            import aep
            if self.powerCurve is self.specifiedPowerCurve:
                self.windSpeedAt85pctX1pnt5 = self.specifiedPowerCurve.getThresholdWindSpeed()
            if hasattr(self.datasetConfigs[0].data,"analysedDirections"):
                self.analysedDirectionSectors = self.datasetConfigs[0].data.analysedDirections # assume a single for now.
            if len(self.powerCurve.powerCurveLevels) != 0:
                self.aepCalc,self.aepCalcLCB = aep.run(self,self.relativePath.convertToAbsolutePath(self.config.nominalWindSpeedDistribution), self.allMeasuredPowerCurve)
                if self.turbRenormActive:
                    self.turbCorrectedAepCalc,self.turbCorrectedAepCalcLCB = aep.run(self,self.relativePath.convertToAbsolutePath(self.config.nominalWindSpeedDistribution), self.allMeasuredTurbCorrectedPowerCurve)
            else:
                self.status.addMessage("A specified power curve is required for AEP calculation. No specified curve defined.")
        if len(self.sensitivityDataColumns) > 0:
            sens_pow_curve = self.allMeasuredTurbCorrectedPowerCurve if self.turbRenormActive else self.allMeasuredPowerCurve
            sens_pow_column = self.measuredTurbulencePower if self.turbRenormActive else self.actualPower
            sens_pow_interp_column = self.measuredTurbPowerCurveInterp if self.turbRenormActive else self.measuredPowerCurveInterp
            self.interpolatePowerCurve(sens_pow_curve, self.inputHubWindSpeedSource, sens_pow_interp_column)
            self.status.addMessage("Attempting power curve sensitivty analysis for %s power curve..." % sens_pow_curve.name)
            self.performSensitivityAnalysis(sens_pow_curve, sens_pow_column, sens_pow_interp_column)
        
        if self.hasActualPower:
            self.powerCurveScatterMetric = self.calculatePowerCurveScatterMetric(self.allMeasuredPowerCurve, self.actualPower, self.dataFrame.index, print_to_console = True)
            self.dayTimePowerCurveScatterMetric = self.calculatePowerCurveScatterMetric(self.dayTimePowerCurve, self.actualPower, self.dataFrame.index[self.getFilter(11)])
            self.nightTimePowerCurveScatterMetric = self.calculatePowerCurveScatterMetric(self.nightTimePowerCurve, self.actualPower, self.dataFrame.index[self.getFilter(12)])
            if self.turbRenormActive:
                self.powerCurveScatterMetricAfterTiRenorm = self.calculatePowerCurveScatterMetric(self.allMeasuredTurbCorrectedPowerCurve, self.measuredTurbulencePower, self.dataFrame.index, print_to_console = True)
            self.powerCurveScatterMetricByWindSpeed = self.calculateScatterMetricByWindSpeed(self.allMeasuredPowerCurve, self.actualPower)
            if self.turbRenormActive:
                self.powerCurveScatterMetricByWindSpeedAfterTiRenorm = self.calculateScatterMetricByWindSpeed(self.allMeasuredTurbCorrectedPowerCurve, self.measuredTurbulencePower)
            self.iec_2005_cat_A_power_curve_uncertainty()
        self.status.addMessage("Total of %.3f hours of data used in analysis." % self.hours)
        self.status.addMessage("Complete")

    def auto_activate_corrections(self):
        self.status.addMessage("Automatically activating corrections based on availabe data.")
        save_conf = False
        if self.hasDensity:
            self.config.densityCorrectionActive = True
            self.status.addMessage("Density Correction activated.")
            save_conf = True
        if self.hubTurbulence in self.dataFrame.columns:
            self.config.turbRenormActive = True
            self.status.addMessage("TI Renormalisation activated.")
            save_conf = True
        if self.rewsDefined:
            self.config.rewsActive = True
            self.status.addMessage("REWS activated.")
            save_conf = True
        if (type(self.config.specifiedPowerDeviationMatrix) in (str, unicode)) and (len(self.config.specifiedPowerDeviationMatrix) > 0):
            self.config.powerDeviationMatrixActive = True
            self.status.addMessage("PDM activated.")
            save_conf = True
        if save_conf:
            self.config.save()

    def applyRemainingFilters(self):

        print "Apply derived filters (filters which depend on calculated columns)"

        for dataSetConf in self.datasetConfigs:

            print dataSetConf.name

            if self.anyFiltersRemaining(dataSetConf):

                print "Applying Remaining Filters"

                print "Extracting dataset data"

                #print "KNOWN BUG FOR CONCURRENT DATASETS"

                datasetStart = dataSetConf.timeStamps[0]
                datasetEnd = dataSetConf.timeStamps[-1]

                print "Start: %s" % datasetStart
                print "End: %s" % datasetEnd

                mask = self.dataFrame[self.timeStamp] > datasetStart
                mask = mask & (self.dataFrame[self.timeStamp] < datasetEnd)
                mask = mask & (self.dataFrame[self.nameColumn] == dataSetConf.name)

                dateRangeDataFrame = self.dataFrame.loc[mask, :]

                self.dataFrame = self.dataFrame.drop(dateRangeDataFrame.index)

                print "Filtering Extracted Data"
                d = dataSetConf.data.filterDataFrame(dateRangeDataFrame, dataSetConf.filters)

                print "(Re)inserting filtered data "
                self.dataFrame = self.dataFrame.append(d)

                if len([filter for filter in dataSetConf.filters if ((not filter.applied) & (filter.active))]) > 0:
                    print [str(filter) for filter in dataSetConf.filters if ((not filter.applied) & (filter.active))]
                    raise Exception("Filters have not been able to be applied!")

            else:

                print "No filters left to apply"

    def anyFiltersRemaining(self, dataSetConf):

        for datasetFilter in dataSetConf.filters:
            if not datasetFilter.applied:
                return True

        return False

    def defineInnerRange(self, config):

        self.innerRangeLowerTurbulence = config.innerRangeLowerTurbulence
        self.innerRangeUpperTurbulence = config.innerRangeUpperTurbulence
        self.innerRangeCenterTurbulence = 0.5 * self.innerRangeLowerTurbulence + 0.5 * self.innerRangeUpperTurbulence

        if self.hasShear:
            self.innerRangeLowerShear = config.innerRangeLowerShear
            self.innerRangeUpperShear = config.innerRangeUpperShear
            self.innerRangeCenterShear = 0.5 * self.innerRangeLowerShear + 0.5 * self.innerRangeUpperShear

    def loadData(self, config, rotorGeometry):

        self.residualWindSpeedMatrices = {}
        self.datasetConfigs = []

        for i in range(len(config.datasets)):


            if not isinstance(config.datasets[i],configuration.DatasetConfiguration):
                datasetConfig = configuration.DatasetConfiguration(self.relativePath.convertToAbsolutePath(config.datasets[i]))
            else:
                datasetConfig = config.datasets[i]

            data = dataset.Dataset(datasetConfig, rotorGeometry, config)

            if hasattr(data,"calibrationCalculator"):
                self.calibrations.append( (datasetConfig,data.calibrationCalculator ) )

            datasetConfig.timeStamps = data.dataFrame.index
            datasetConfig.data = data
            self.datasetConfigs.append(datasetConfig)

            if i == 0:

                #analysis 'inherits' timestep from first data set. Subsequent datasets will be checked for consistency
                self.timeStepInSeconds = datasetConfig.timeStepInSeconds

                #copy column names from dataset
                self.timeStamp = data.timeStamp
                self.hubWindSpeed = data.hubWindSpeed
                self.hubTurbulence = data.hubTurbulence
                self.hubDensity = data.hubDensity
                self.shearExponent = data.shearExponent

                if data.rewsDefined:
                    self.profileRotorWindSpeed = data.profileRotorWindSpeed
                    self.profileHubWindSpeed = data.profileHubWindSpeed
                    self.profileHubToRotorRatio = data.profileHubToRotorRatio
                    self.profileHubToRotorDeviation = data.profileHubToRotorDeviation

                self.actualPower = data.actualPower
                self.residualWindSpeed = data.residualWindSpeed

                self.dataFrame = data.dataFrame
                self.hasActualPower = data.hasActualPower
                self.hasAllPowers = data.hasAllPowers
                self.hasShear = data.hasShear
                self.hasDensity = data.hasDensity
                self.hasDirection = data.hasDirection
                self.rewsDefined = data.rewsDefined
                self.sensitivityDataColumns = data.sensitivityDataColumns

            else:

                if datasetConfig.timeStepInSeconds <> self.timeStepInSeconds:
                    raise Exception ("Dataset time step (%d) does not match analysis (%d) time step" % (datasetConfig.timeStepInSeconds, self.timeStepInSeconds))

                self.dataFrame = self.dataFrame.append(data.dataFrame, ignore_index = True)

                self.hasActualPower = self.hasActualPower & data.hasActualPower
                self.hasAllPowers = self.hasAllPowers & data.hasAllPowers
                self.hasShear = self.hasShear & data.hasShear
                self.hasDensity = self.hasDensity & data.hasDensity
                self.rewsDefined = self.rewsDefined & data.rewsDefined

            self.residualWindSpeedMatrices[data.name] = data.residualWindSpeedMatrix

        self.timeStampHours = float(self.timeStepInSeconds) / 3600.0

    def generate_unique_dset_ids(self):
        dset_ids = {}
        for conf in self.datasetConfigs:
            ids = {}
            ids['Configuration'] = hash_file_contents(conf.path)
            ids['Time Series'] = hash_file_contents(conf.data.relativePath.convertToAbsolutePath(conf.inputTimeSeriesPath))
            dset_ids[conf.name] = ids
        return dset_ids

    def selectPowerCurve(self, powerCurveMode):

        if powerCurveMode == "Specified":

            return self.specifiedPowerCurve

        elif powerCurveMode == "InnerMeasured":

            if self.hasActualPower and self.hasShear:
                return self.innerMeasuredPowerCurve
            elif not self.hasActualPower:
                raise Exception("Cannot use inner measured power curvve: Power data not specified")
            elif not self.hasShear:
                raise Exception("Cannot use inner measured power curvve: Shear data not specified")

        elif powerCurveMode == "InnerTurbulenceMeasured":

            if self.hasActualPower:
                return self.innerTurbulenceMeasuredPowerCurve
            else:
                raise Exception("Cannot use inner measured power curvve: Power data not specified")

        elif powerCurveMode == "OuterMeasured":

            if self.hasActualPower and self.hasShear:
                return self.outerMeasuredPowerCurve
            else:
                raise Exception("Cannot use outer measured power curvve: Power data not specified")

        elif powerCurveMode == "OuterTurbulenceMeasured":

            if self.hasActualPower:
                return self.outerTurbulenceMeasuredPowerCurve
            else:
                raise Exception("Cannot use outer measured power curvve: Power data not specified")

        elif powerCurveMode == "AllMeasured":

            if self.hasActualPower:
                return self.allMeasuredPowerCurve
            else:
                raise Exception("Cannot use all measured power curvve: Power data not specified")

        else:
            raise Exception("Unrecognised power curve mode: %s" % powerCurveMode)

    def getFilter(self, mode = None):

        if mode == None:
            mode = self.getFilterMode()

        if self.baseLineMode == "Hub":
            mask = self.dataFrame[self.inputHubWindSpeed].notnull()
        elif self.baseLineMode == "Measured":
            mask = self.dataFrame[self.actualPower] > 0
        else:
            raise Exception("Unrecognised baseline mode: %s" % self.baseLineMode)

        innerTurbMask = (self.dataFrame[self.hubTurbulence] >= self.innerRangeLowerTurbulence) & (self.dataFrame[self.hubTurbulence] <= self.innerRangeUpperTurbulence)
        if self.hasShear: innerShearMask = (self.dataFrame[self.shearExponent] >= self.innerRangeLowerShear) & (self.dataFrame[self.shearExponent] <= self.innerRangeUpperShear)

        if mode > 0:

            if mode <=3:

                #Inner
                if mode == 1:
                    mask = mask & innerTurbMask & innerShearMask
                elif mode == 2:
                    mask = mask & innerTurbMask
                elif mode == 3:
                    mask = mask & innerShearMask
                else:
                    raise Exception("Unexpected filter mode")

            elif mode <= 6:

                #Outer
                if mode == 4:
                    mask = ~(innerTurbMask & innerShearMask)
                elif mode == 5:
                    mask = ~innerTurbMask
                elif mode == 6:
                    mask = ~innerShearMask
                else:
                    raise Exception("Unexpected filter mode")

            elif mode <= 10:

                innerMask = innerTurbMask & innerShearMask
                mask = mask & (~innerMask)

                if mode == 7:
                    #LowShearLowTurbulence
                    mask = mask & (self.dataFrame[self.shearExponent] <= self.innerRangeCenterShear) & (self.dataFrame[self.hubTurbulence] <= self.innerRangeCenterTurbulence)
                elif mode == 8:
                    #LowShearHighTurbulence
                    mask = mask & (self.dataFrame[self.shearExponent] <= self.innerRangeCenterShear) & (self.dataFrame[self.hubTurbulence] >= self.innerRangeCenterTurbulence)
                elif mode == 9:
                    #HighShearHighTurbulence
                    mask = mask & (self.dataFrame[self.shearExponent] >= self.innerRangeCenterShear) & (self.dataFrame[self.hubTurbulence] >= self.innerRangeCenterTurbulence)
                elif mode == 10:
                    #HighShearLowTurbulence
                    mask = mask & (self.dataFrame[self.shearExponent] >= self.innerRangeCenterShear) & (self.dataFrame[self.hubTurbulence] <= self.innerRangeCenterTurbulence)
                else:
                    raise Exception("Unexpected filter mode")
            
            else:
                if mode == 11:
                    #for day time power curve (between 7am and 8pm)
                    mask = mask & (self.dataFrame[self.timeStamp].dt.hour >= 7) & (self.dataFrame[self.timeStamp].dt.hour <= 20)
                elif mode == 12:
                    #for night time power curve (between 8pm and 7am)
                    mask = mask & ((self.dataFrame[self.timeStamp].dt.hour < 7) | (self.dataFrame[self.timeStamp].dt.hour > 20))
                else:
                    raise Exception("Unexpected filter mode")

        return mask

    def getFilterMode(self):

        if self.filterMode == "Inner":
            return 1
        elif self.filterMode == "InnerTurb":
            return 2
        elif self.filterMode == "InnerShear":
            return 3
        elif self.filterMode == "Outer":
            return 4
        elif self.filterMode == "OuterTurb":
            return 5
        elif self.filterMode == "OuterShear":
            return 6
        elif self.filterMode == "LowShearLowTurbulence":
            return 7
        elif self.filterMode == "LowShearHighTurbulence":
            return 8
        elif self.filterMode == "HighShearHighTurbulence":
            return 9
        elif self.filterMode == "HighShearLowTurbulence":
            return 10
        elif self.filterMode == "All":
            return 0
        elif self.filterMode == "Day":
            return 11
        elif self.filterMode == "Night":
            return 12
        else:
            raise Exception("Unrecognised filter mode: %s" % self.filterMode)

    def interpolatePowerCurve(self, powerCurveLevels, ws_col, interp_power_col):
        self.dataFrame[interp_power_col] = self.dataFrame[ws_col].apply(powerCurveLevels.power)

    def performSensitivityAnalysis(self, power_curve, power_column, interp_pow_column, n_random_tests = 20):

        mask = self.getFilter()
        filteredDataFrame = self.dataFrame[mask]
        
        #calculate significance threshold based on generated random variable
        rand_columns, rand_sensitivity_results = [], []
        for i in range(n_random_tests):
            rand_columns.append('Random ' + str(i + 1))
        filteredDataFrame[rand_columns] = pd.DataFrame(np.random.rand(len(filteredDataFrame),n_random_tests), columns=rand_columns, index = filteredDataFrame.index)
        for col in rand_columns:
            variation_metric = self.calculatePowerCurveSensitivity(filteredDataFrame, power_curve, col, power_column, interp_pow_column)[1]
            rand_sensitivity_results.append(variation_metric)
        self.sensitivityAnalysisThreshold = np.mean(rand_sensitivity_results)
        print "\nSignificance threshold for power curve variation metric is %.2f%%."  % (self.sensitivityAnalysisThreshold * 100.)
        self.status.addMessage("\nSignificance threshold for power curve variation metric is %.2f%%."  % (self.sensitivityAnalysisThreshold * 100.))
        filteredDataFrame.drop(rand_columns, axis = 1, inplace = True)
        
        #sensitivity to time of day, time of year, time elapsed in test
        filteredDataFrame['Days Elapsed In Test'] = (filteredDataFrame[self.timeStamp] - filteredDataFrame[self.timeStamp].min()).dt.days
        filteredDataFrame['Hours From Noon'] = np.abs(filteredDataFrame[self.timeStamp].dt.hour - 12)
        filteredDataFrame['Hours From Midnight'] = np.minimum(filteredDataFrame[self.timeStamp].dt.hour, np.abs(24 - filteredDataFrame[self.timeStamp].dt.hour))
        filteredDataFrame['Days From 182nd Day Of Year'] = np.abs(filteredDataFrame[self.timeStamp].dt.dayofyear - 182)
        filteredDataFrame['Days From December Solstice'] = filteredDataFrame[self.timeStamp].apply(lambda x: x.replace(day = 22, month = 12)) - filteredDataFrame[self.timeStamp]
        filteredDataFrame['Days From December Solstice'] = np.minimum(np.abs(filteredDataFrame['Days From December Solstice'].dt.days), 365 - np.abs(filteredDataFrame['Days From December Solstice'].dt.days))
        
        #for col in (self.sensitivityDataColumns + ['Days Elapsed In Test','Hours From Noon','Days From 182nd Day Of Year']):
        for col in (list(filteredDataFrame.columns)): # if we want to do the sensitivity analysis for all columns in the dataframe...
            print "\nAttempting to compute sensitivity of power curve to %s..." % col
            try:
                self.powerCurveSensitivityResults[col], self.powerCurveSensitivityVariationMetrics.loc[col, 'Power Curve Variation Metric'] = self.calculatePowerCurveSensitivity(filteredDataFrame, power_curve, col, power_column, interp_pow_column)
                print "Variation of power curve with respect to %s is %.2f%%." % (col, self.powerCurveSensitivityVariationMetrics.loc[col, 'Power Curve Variation Metric'] * 100.)
                if self.powerCurveSensitivityVariationMetrics.loc[col,'Power Curve Variation Metric'] == 0:
                    self.powerCurveSensitivityVariationMetrics.drop(col, axis = 1, inplace = True)
            except:
                print "Could not run sensitivity analysis for %s." % col
        self.powerCurveSensitivityVariationMetrics.loc['Significance Threshold', 'Power Curve Variation Metric'] = self.sensitivityAnalysisThreshold
        self.powerCurveSensitivityVariationMetrics.sort('Power Curve Variation Metric', ascending = False, inplace = True)
    
    def calculatePowerCurveSensitivity(self, dataFrame, power_curve, dataColumn, power_column, interp_pow_column):
        
        dataFrame['Energy MWh'] = (dataFrame[power_column] * (float(self.timeStepInSeconds) / 3600.)).astype('float')
        
        from collections import OrderedDict
        self.sensitivityLabels = OrderedDict([("V Low","#0000ff"), ("Low","#4400bb"), ("Medium","#880088"), ("High","#bb0044"), ("V High","#ff0000")]) #categories to split data into using data_column and colour to plot
        cutOffForCategories = list(np.arange(0.,1.,1./len(self.sensitivityLabels.keys()))) + [1.]
        
        minCount = len(self.sensitivityLabels.keys()) * 4 #at least 4 data points for each category for a ws bin to be valid
        
        wsBinnedCount = dataFrame[['Wind Speed Bin', dataColumn]].groupby('Wind Speed Bin').count()
        validWsBins = wsBinnedCount.index[wsBinnedCount[dataColumn] > minCount] #ws bins that have enough data for the sensitivity analysis

        dataFrame['Bin'] = np.nan #pre-allocating
        dataFrame['Power Delta kW'] = dataFrame[power_column] - dataFrame[interp_pow_column]
        dataFrame['Energy Delta MWh'] = dataFrame['Power Delta kW'] * (float(self.timeStepInSeconds) / 3600.)
        
        for wsBin in dataFrame['Wind Speed Bin'].unique(): #within each wind speed bin, bin again by the categorising by sensCol
            if wsBin in validWsBins:
                try:
                    filt = dataFrame['Wind Speed Bin'] == wsBin
                    dataFrame.loc[filt,'Bin'] = pd.qcut(dataFrame[dataColumn][filt], cutOffForCategories, labels = self.sensitivityLabels.keys())
                except:
                    print "\tCould not categorise data by %s for WS bin %s." % (dataColumn, wsBin)
        
        sensitivityResults = dataFrame[[power_column, 'Energy MWh', 'Wind Speed Bin','Bin', 'Power Delta kW', 'Energy Delta MWh']].groupby(['Wind Speed Bin','Bin']).agg({power_column: np.mean, 'Energy MWh': np.sum, 'Wind Speed Bin': len, 'Power Delta kW': np.mean, 'Energy Delta MWh': np.sum})
#        sensitivityResults['Energy Delta MWh'], sensitivityResults['Power Delta kW'] = np.nan, np.nan #pre-allocate
#        for i in sensitivityResults.index:
#            #sensitivityResults.loc[i, 'Power Delta kW'] = sensitivityResults.loc[i, power_column] - power_curve.powerCurveLevels.loc[i[0], power_column]
#            sensitivityResults.loc[i, 'Energy Delta MWh'] = sensitivityResults.loc[i, 'Power Delta kW'] * power_curve.powerCurveLevels.loc[i[0], 'Data Count'] * (float(self.timeStepInSeconds) / 3600.)
        
        return sensitivityResults.rename(columns = {'Wind Speed Bin':'Data Count'}), np.abs(sensitivityResults['Energy Delta MWh']).sum() / (power_curve.powerCurveLevels[power_column] * power_curve.powerCurveLevels['Data Count'] * (float(self.timeStepInSeconds) / 3600.)).sum()

    def calculateMeasuredPowerCurve(self, mode, cutInWindSpeed, cutOutWindSpeed, ratedPower, powerColumn, name, required = False):
        
        print "Calculating %s power curve." % name        
        
        mask = (self.dataFrame[powerColumn] > (self.ratedPower * -.25)) & (self.dataFrame[self.inputHubWindSpeed] > 0) & (self.dataFrame[self.hubTurbulence] > 0) & self.getFilter(mode)
        
        filteredDataFrame = self.dataFrame[mask]
        
        print "%s rows of data being used for %s power curve." % (len(filteredDataFrame), name)

        #storing power curve in a dataframe as opposed to dictionary
        dfPowerLevels = filteredDataFrame[[powerColumn, self.inputHubWindSpeed, self.hubTurbulence]].groupby(filteredDataFrame[self.windSpeedBin]).aggregate(self.aggregations.average)
        powerStdDev = filteredDataFrame[[powerColumn, self.inputHubWindSpeed]].groupby(filteredDataFrame[self.windSpeedBin]).std().rename(columns={powerColumn:self.powerStandDev})[self.powerStandDev]

        dfDataCount = filteredDataFrame[powerColumn].groupby(filteredDataFrame[self.windSpeedBin]).agg({self.dataCount:'count'})
        if not all(dfPowerLevels.index == dfDataCount.index):
            raise Exception("Index of aggregated data count and mean quantities for measured power curve do not match.")
        dfPowerLevels = dfPowerLevels.join(dfDataCount, how = 'inner')
        dfPowerLevels = dfPowerLevels.join(powerStdDev, how = 'inner')
        dfPowerLevels.dropna(inplace = True)
        
        if self.powerCoeff in filteredDataFrame.columns:
            dfPowerCoeff = filteredDataFrame[self.powerCoeff].groupby(filteredDataFrame[self.windSpeedBin]).aggregate(self.aggregations.average)
        else:
            dfPowerCoeff = None

        if len(dfPowerLevels.index) != 0:
            #padding
            # To deal with data missing between cutOut and last measured point:
            # Specified : Use specified rated power
            # Last : Use last observed power
            # Linear : linearly interpolate from last observed power at last observed ws to specified power at specified ws.
            maxTurb = dfPowerLevels[self.hubTurbulence].max()
            minTurb = dfPowerLevels[self.hubTurbulence].min()
            
            powerCurvePadder = PadderFactory().generate(self.powerCurvePaddingMode, powerColumn, self.inputHubWindSpeed, self.hubTurbulence, self.dataCount)

            powerLevels = powerCurvePadder.pad(dfPowerLevels,cutInWindSpeed,cutOutWindSpeed,ratedPower, self.windSpeedBins)

            if dfPowerCoeff is not None:
                powerLevels[self.powerCoeff] = dfPowerCoeff

            return turbine.PowerCurve(powerLevels, self.referenceDensity, self.rotorGeometry, powerColumn,
                                      self.hubTurbulence, wsCol = self.inputHubWindSpeed, countCol = self.dataCount,
                                            turbulenceRenormalisation = (self.turbRenormActive if powerColumn != self.turbulencePower else False), 
                                            name = name, interpolationMode = self.interpolationMode, required = required)

    def calculatePowerDeviationMatrix(self, power, filterMode, windBin = None, turbBin = None):
        if windBin is None:
            windBin = self.windSpeedBin
        if turbBin is None:
            turbBin = self.turbulenceBin

        mask = (self.dataFrame[self.actualPower] > 0) & (self.dataFrame[power] > 0)
        mask = mask & self.getFilter(filterMode)

        filteredDataFrame = self.dataFrame[mask]
        filteredDataFrame.is_copy = False
        filteredDataFrame[self.powerDeviation] = (filteredDataFrame[self.actualPower] - filteredDataFrame[power]) / filteredDataFrame[power]

        devMatrix = DeviationMatrix(filteredDataFrame[self.powerDeviation].groupby([filteredDataFrame[windBin], filteredDataFrame[turbBin]]).aggregate(self.aggregations.average),
                                    filteredDataFrame[self.powerDeviation].groupby([filteredDataFrame[windBin], filteredDataFrame[turbBin]]).count())

        return devMatrix

    def calculateREWSMatrix(self, filterMode):

        mask = self.dataFrame[self.inputHubWindSpeed] > 0.0
        mask = mask & self.getFilter(filterMode)

        filteredDataFrame = self.dataFrame[mask]

        rewsMatrix = DeviationMatrix(filteredDataFrame[self.profileHubToRotorDeviation].groupby([filteredDataFrame[self.windSpeedBin], filteredDataFrame[self.turbulenceBin]]).aggregate(self.aggregations.average),
                                    filteredDataFrame[self.profileHubToRotorDeviation].groupby([filteredDataFrame[self.windSpeedBin], filteredDataFrame[self.turbulenceBin]]).count())

        return rewsMatrix

    def calculatePowerCurveScatterMetric(self, measuredPowerCurve, powerColumn, rows, print_to_console = False): #this calculates a metric for the scatter of the all measured PC
        
        try:
            energyDiffMWh = np.abs((self.dataFrame.loc[rows, powerColumn] - self.dataFrame.loc[rows, self.inputHubWindSpeed].apply(measuredPowerCurve.power)) * (float(self.timeStepInSeconds) / 3600.))
            energyMWh = self.dataFrame.loc[rows, powerColumn] * (float(self.timeStepInSeconds) / 3600.)
            powerCurveScatterMetric = energyDiffMWh.sum() / energyMWh.sum()
            print "%s scatter metric is %.2f%%." % (measuredPowerCurve.name, powerCurveScatterMetric * 100.)
            if print_to_console:
                self.status.addMessage("\n%s scatter metric is %.3f%%." % (measuredPowerCurve.name, powerCurveScatterMetric * 100.))
            return powerCurveScatterMetric
        except:
            print "Could not calculate power curve scatter metric."
            return np.nan
            
    def calculateScatterMetricByWindSpeed(self, measuredPowerCurve, powerColumn):
        index = self.dataFrame[self.windSpeedBin].unique()
        index.sort()
        df = pd.DataFrame(index = index, columns = ['Scatter Metric'])
        for ws in df.index:
            if ws >= measuredPowerCurve.cutInWindSpeed:
                rows = self.dataFrame[self.inputHubWindSpeed] == ws
                df.loc[ws, 'Scatter Metric'] = self.calculatePowerCurveScatterMetric(measuredPowerCurve, powerColumn, rows)
        return df.dropna()

    def calculate_pcwg_error_fields(self):
        self.calculate_anonymous_values()
        self.pcwgErrorBaseline = 'Baseline Error'
        self.pcwgErrorCols = [self.pcwgErrorBaseline]
        self.dataFrame[self.pcwgErrorBaseline] = self.dataFrame[self.hubPower] - self.dataFrame[self.actualPower]
        if self.turbRenormActive:
            self.pcwgErrorTurbRenor = 'TI Renormalisation Error'
            self.dataFrame[self.pcwgErrorTurbRenor] = self.dataFrame[self.turbulencePower] - self.dataFrame[self.actualPower]
            self.pcwgErrorCols.append(self.pcwgErrorTurbRenor)
        if self.rewsActive:
            self.pcwgErrorRews = 'REWS Error'
            self.dataFrame[self.pcwgErrorRews] = self.dataFrame[self.rewsPower] - self.dataFrame[self.actualPower]
            self.pcwgErrorCols.append(self.pcwgErrorRews)
        if (self.turbRenormActive and self.rewsActive):
            self.pcwgErrorTiRewsCombined = 'Combined TI Renorm and REWS Error'
            self.dataFrame[self.pcwgErrorTiRewsCombined] = self.dataFrame[self.combinedPower] - self.dataFrame[self.actualPower]
            self.pcwgErrorCols.append(self.pcwgErrorTiRewsCombined)
        if self.powerDeviationMatrixActive:
            self.pcwgErrorPdm = 'PDM Error'
            self.dataFrame[self.pcwgErrorPdm] = self.dataFrame[self.powerDeviationMatrixPower] - self.dataFrame[self.actualPower]
            self.pcwgErrorCols.append(self.pcwgErrorPdm)
        self.powerCurveCompleteBins = self.powerCurve.powerCurveLevels.index[self.powerCurve.powerCurveLevels[self.dataCount] > 0]
        self.pcwgErrorValid = 'Baseline Power Curve WS Bin Complete'
        self.dataFrame[self.pcwgErrorValid] = self.dataFrame[self.windSpeedBin].isin(self.powerCurveCompleteBins)
    
    def calculate_pcwg_overall_metrics(self):
        self.overall_pcwg_err_metrics = {}
        NME, NMAE, data_count = self._calculate_pcwg_error_metric(self.pcwgErrorBaseline)
        self.overall_pcwg_err_metrics[self.dataCount] = data_count
        self.overall_pcwg_err_metrics['Baseline NME'] = NME
        self.overall_pcwg_err_metrics['Baseline NMAE'] = NMAE
        if self.turbRenormActive:
            NME, NMAE, _ = self._calculate_pcwg_error_metric(self.pcwgErrorTurbRenor)
            self.overall_pcwg_err_metrics['TI Renorm NME'] = NME
            self.overall_pcwg_err_metrics['TI Renorm NMAE'] = NMAE
        if self.rewsActive:
            NME, NMAE, _ = self._calculate_pcwg_error_metric(self.pcwgErrorRews)
            self.overall_pcwg_err_metrics['REWS NME'] = NME
            self.overall_pcwg_err_metrics['REWS NMAE'] = NMAE
        if (self.turbRenormActive and self.rewsActive):
            NME, NMAE, _ = self._calculate_pcwg_error_metric(self.pcwgErrorTiRewsCombined)
            self.overall_pcwg_err_metrics['REWS and TI Renorm NME'] = NME
            self.overall_pcwg_err_metrics['REWS and TI Renorm NMAE'] = NMAE
        if self.powerDeviationMatrixActive:
            NME, NMAE, _ = self._calculate_pcwg_error_metric(self.pcwgErrorPdm)
            self.overall_pcwg_err_metrics['PDM NME'] = NME
            self.overall_pcwg_err_metrics['PDM NMAE'] = NMAE
            
    def calculate_pcwg_binned_metrics(self):
        reporting_bins = [self.normalisedWSBin, self.hourOfDay, self.calendarMonth, self.pcwgFourCellMatrixGroup, self.pcwgRange]
        if self.hasDirection:
            reporting_bins.append(self.pcwgDirectionBin)
        self.binned_pcwg_err_metrics = {}
        for bin_col_name in reporting_bins:
            self.binned_pcwg_err_metrics[bin_col_name] = {}
            self.binned_pcwg_err_metrics[bin_col_name][self.pcwgErrorBaseline] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorBaseline, bin_col_name)
            if self.turbRenormActive:
                self.binned_pcwg_err_metrics[bin_col_name][self.pcwgErrorTurbRenor] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorTurbRenor, bin_col_name)
            if self.rewsActive:
                self.binned_pcwg_err_metrics[bin_col_name][self.pcwgErrorRews] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorRews, bin_col_name)
            if (self.turbRenormActive and self.rewsActive):
                self.binned_pcwg_err_metrics[bin_col_name][self.pcwgErrorTiRewsCombined] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorTiRewsCombined, bin_col_name)
            if self.powerDeviationMatrixActive:
                self.binned_pcwg_err_metrics[bin_col_name][self.pcwgErrorPdm] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorPdm, bin_col_name)
        #Using Inner and Outer range data only to calculate error metrics binned by normalised WS
        bin_col_name = self.normalisedWSBin
        for pcwg_range in ['Inner', 'Outer']:
            dict_key = bin_col_name + ' ' + pcwg_range + ' Range'
            self.binned_pcwg_err_metrics[dict_key] = {}
            self.binned_pcwg_err_metrics[dict_key][self.pcwgErrorBaseline] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorBaseline, bin_col_name, pcwg_range = pcwg_range)
            if self.turbRenormActive:
                self.binned_pcwg_err_metrics[dict_key][self.pcwgErrorTurbRenor] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorTurbRenor, bin_col_name, pcwg_range = pcwg_range)
            if self.rewsActive:
                self.binned_pcwg_err_metrics[dict_key][self.pcwgErrorRews] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorRews, bin_col_name, pcwg_range = pcwg_range)
            if (self.turbRenormActive and self.rewsActive):
                self.binned_pcwg_err_metrics[dict_key][self.pcwgErrorTiRewsCombined] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorTiRewsCombined, bin_col_name, pcwg_range = pcwg_range)
            if self.powerDeviationMatrixActive:
                self.binned_pcwg_err_metrics[dict_key][self.pcwgErrorPdm] = self._calculate_pcwg_error_metric_by_bin(self.pcwgErrorPdm, bin_col_name, pcwg_range = pcwg_range)
            
    def _calculate_pcwg_error_metric_by_bin(self, candidate_error, bin_col_name, pcwg_range = 'All'):
        def sum_abs(x):
            return x.abs().sum()
        if pcwg_range == 'All':
            grouped = self.dataFrame.loc[self.dataFrame[self.pcwgErrorValid], :].groupby(bin_col_name)
        elif pcwg_range == 'Inner':
            grouped = self.dataFrame.loc[np.logical_and(self.dataFrame[self.pcwgErrorValid], (self.dataFrame[self.pcwgRange] == 'Inner')), :].groupby(bin_col_name)
        elif pcwg_range == 'Outer':
            grouped = self.dataFrame.loc[np.logical_and(self.dataFrame[self.pcwgErrorValid], (self.dataFrame[self.pcwgRange] == 'Outer')), :].groupby(bin_col_name)
        else:
            raise Exception('Unrecognised pcwg_range argument %s passed to Analysis._calculate_pcwg_error_metric_by_bin() method. Must be Inner, Outer or All.' % pcwg_range)
        agg = grouped.agg({candidate_error: ['sum', sum_abs, 'count'], self.actualPower: 'sum'})
        agg.loc[:, (candidate_error, 'NME')] = agg.loc[:, (candidate_error, 'sum')] / agg.loc[:, (self.actualPower, 'sum')]
        agg.loc[:, (candidate_error, 'NMAE')] = agg.loc[:, (candidate_error, 'sum_abs')] / agg.loc[:, (self.actualPower, 'sum')]
        return agg.loc[:, candidate_error].drop(['sum', 'sum_abs'], axis = 1).rename(columns = {'count': self.dataCount})
    
    def _calculate_pcwg_error_metric(self, candidate_error):
        data_count = len(self.dataFrame.loc[self.dataFrame[self.pcwgErrorValid], candidate_error].dropna())
        NME = (self.dataFrame.loc[self.dataFrame[self.pcwgErrorValid], candidate_error].sum() / self.dataFrame.loc[self.dataFrame[self.pcwgErrorValid], self.actualPower].sum())
        NMAE = (np.abs(self.dataFrame.loc[self.dataFrame[self.pcwgErrorValid], candidate_error]).sum() / self.dataFrame.loc[self.dataFrame[self.pcwgErrorValid], self.actualPower].sum())
        return NME, NMAE, data_count

    def iec_2005_cat_A_power_curve_uncertainty(self):
        if self.turbRenormActive:
            pc = self.allMeasuredTurbCorrectedPowerCurve.powerCurveLevels
            pow_col = self.measuredTurbulencePower
        else:
            pc = self.allMeasuredPowerCurve.powerCurveLevels
            pow_col = self.actualPower
        #pc['frequency'] = pc[self.dataCount] / pc[self.dataCount].sum()
        pc['s_i'] = pc[self.powerStandDev] / (pc[self.dataCount]**0.5) #from IEC 2005
        unc_MWh = (np.abs(pc['s_i']) * (pc[self.dataCount] / 6.)).sum()
        test_MWh = (np.abs(pc[pow_col]) * (pc[self.dataCount] / 6.)).sum()
        self.categoryAUncertainty = unc_MWh / test_MWh
        self.status.addMessage("Power curve category A uncertainty: %.3f%%" % (self.categoryAUncertainty * 100.0))

    def report(self, path,version="unknown", report_power_curve = True):

        report = reporting.report(self.windSpeedBins, self.turbulenceBins, version, report_power_curve = report_power_curve)
        report.report(path, self)

    def anonym_report(self, path, version="Unknown", scatter = False, deviationMatrix = True):

        if not self.hasActualPower:
            raise Exception("Anonymous report can only be generated if analysis has actual power data")

        if deviationMatrix:
            self.calculate_anonymous_values()
        else:
            self.normalisedWindSpeedBins = []

        report = reporting.AnonReport(targetPowerCurve = self.powerCurve,
                                      wind_bins = self.normalisedWindSpeedBins,
                                      turbulence_bins = self.turbulenceBins,
                                      version= version)

        report.report(path, self, powerDeviationMatrix = deviationMatrix, scatterMetric= scatter)

    def pcwg_share_metrics_calc(self):
        if self.powerCurveMode != "InnerMeasured":
            raise Exception("Power Curve Mode must be set to Inner to export PCWG Sharing Initiative 1 Report.")
        else:
            self.calculate_pcwg_error_fields()
            self.calculate_pcwg_overall_metrics()
            self.calculate_pcwg_binned_metrics()

    def pcwg_data_share_report(self, version = 'Unknown', output_fname = (os.getcwd() + os.sep + 'Data Sharing Initiative 1 Report.xls')):
        from data_sharing_reports import pcwg_share1_rpt
        rpt = pcwg_share1_rpt(self, version = version, output_fname = output_fname)
        rpt.report()

    def calculate_anonymous_values(self):

        allFilterMode = 0
                
        self.normalisedWSBin = 'Normalised WS Bin Centre'
        firstNormWSbin = 0.05
        lastNormWSbin = 2.95
        normWSstep = 0.1
        self.normalisedWindSpeedBins = binning.Bins(firstNormWSbin, normWSstep, lastNormWSbin)
        self.dataFrame[self.normalisedWSBin] = (self.dataFrame[self.normalisedWS]).map(self.normalisedWindSpeedBins.binCenter)

        if self.hasDirection:
            self.pcwgDirectionBin = 'Wind Direction Bin Centre'
            dir_bin_width = 10.
            wdir_centre_first_bin = 0.
            self.pcwgWindDirBins = binning.Bins(wdir_centre_first_bin, dir_bin_width, 350.)
            self.dataFrame[self.pcwgDirectionBin] = (self.dataFrame[self.windDirection] - wdir_centre_first_bin) / dir_bin_width
            self.dataFrame[self.pcwgDirectionBin] = np.round(self.dataFrame[self.pcwgDirectionBin], 0) * dir_bin_width + wdir_centre_first_bin
            self.dataFrame[self.pcwgDirectionBin] = (self.dataFrame[self.pcwgDirectionBin] + 360) % 360

        self.pcwgFourCellMatrixGroup = 'PCWG Four Cell WS-TI Matrix Group'
        self.dataFrame[self.pcwgFourCellMatrixGroup] = np.nan
        filt = (self.dataFrame[self.normalisedWS] >= 0.5) & (self.dataFrame[self.hubTurbulence] >= self.innerRangeUpperTurbulence)
        self.dataFrame.loc[filt, self.pcwgFourCellMatrixGroup] = 'HWS-HTI'
        filt = (self.dataFrame[self.normalisedWS] < 0.5) & (self.dataFrame[self.hubTurbulence] >= self.innerRangeUpperTurbulence)
        self.dataFrame.loc[filt, self.pcwgFourCellMatrixGroup] = 'LWS-HTI'
        filt = (self.dataFrame[self.normalisedWS] >= 0.5) & (self.dataFrame[self.hubTurbulence] <= self.innerRangeLowerTurbulence)
        self.dataFrame.loc[filt, self.pcwgFourCellMatrixGroup] = 'HWS-LTI'
        filt = (self.dataFrame[self.normalisedWS] < 0.5) & (self.dataFrame[self.hubTurbulence] <= self.innerRangeLowerTurbulence)
        self.dataFrame.loc[filt, self.pcwgFourCellMatrixGroup] = 'LWS-LTI'
        
        self.pcwgRange = 'PCWG Range (Inner or Outer)'
        self.dataFrame[self.pcwgRange] = np.nan
        self.dataFrame.loc[self.getFilter(1), self.pcwgRange] = 'Inner'
        self.dataFrame.loc[self.getFilter(4), self.pcwgRange] = 'Outer'
        
        self.hourOfDay = 'Hour Of Day'
        self.dataFrame[self.hourOfDay] = self.dataFrame[self.timeStamp].dt.hour
        self.calendarMonth = 'Calendar Month'
        self.dataFrame[self.calendarMonth] = self.dataFrame[self.timeStamp].dt.month

        self.normalisedHubPowerDeviations = self.calculatePowerDeviationMatrix(self.hubPower, allFilterMode
                                                                               ,windBin = self.normalisedWSBin
                                                                               ,turbBin = self.turbulenceBin)

        if self.config.turbRenormActive:
            self.normalisedTurbPowerDeviations = self.calculatePowerDeviationMatrix(self.turbulencePower, allFilterMode
                                                                                   ,windBin = self.normalisedWSBin
                                                                                   ,turbBin = self.turbulenceBin)
        else:
            self.normalisedTurbPowerDeviations = None


    def calculateBase(self):

        if self.baseLineMode == "Hub":
            if self.powerCurve is None:
                exc_str = "%s Power Curve has not been calculated successfully." % self.powerCurveMode
                if self.powerCurveMode == 'InnerMeasured':
                    exc_str += " Check Inner Range settings."
                raise Exception(exc_str)
            self.dataFrame[self.basePower] = self.dataFrame.apply(PowerCalculator(self.powerCurve, self.inputHubWindSpeed).power, axis=1)
        elif self.baseLineMode == "Measured":
            if self.hasActualPower:
                self.dataFrame[self.basePower] = self.dataFrame[self.actualPower]
            else:
                raise Exception("You must specify a measured power data column if using the 'Measured' baseline mode")
        else:
            raise Exception("Unkown baseline mode: % s" % self.baseLineMode)

        self.baseYield = self.dataFrame[self.getFilter()][self.basePower].sum() * self.timeStampHours

    def calculateCp(self):
        area = np.pi*(self.config.diameter/2.0)**2
        a = 1000*self.dataFrame[self.actualPower]/(0.5*self.dataFrame[self.hubDensity] *area*np.power(self.dataFrame[self.hubWindSpeed],3))
        b = 1000*self.dataFrame[self.actualPower]/(0.5*self.referenceDensity*area*np.power(self.dataFrame[self.densityCorrectedHubWindSpeed],3))
        betzExceed = (len(a[a>16.0/27])*100.0)/len(a)
        if betzExceed > 0.5:
            print "{0:.02}% data points slightly exceed Betz limit - if this number is high, investigate...".format(betzExceed)
        if (abs(a-b) > 0.005).any():
            raise Exception("Density correction has not been applied consistently.")
        return a

    def calculateHub(self):
        self.dataFrame[self.hubPower] = self.dataFrame.apply(PowerCalculator(self.powerCurve, self.inputHubWindSpeed).power, axis=1)
        self.hubYield = self.dataFrame[self.getFilter()][self.hubPower].sum() * self.timeStampHours
        self.hubYieldCount = self.dataFrame[self.getFilter()][self.hubPower].count()
        self.hubDelta = self.hubYield / self.baseYield - 1.0
        self.status.addMessage("Hub Delta: %.3f%% (%d)" % (self.hubDelta * 100.0, self.hubYieldCount))

    def calculateREWS(self):
        self.dataFrame[self.rotorEquivalentWindSpeed] = self.dataFrame[self.inputHubWindSpeed] * self.dataFrame[self.profileHubToRotorRatio]
        self.dataFrame[self.rewsPower] = self.dataFrame.apply(PowerCalculator(self.powerCurve, self.rotorEquivalentWindSpeed).power, axis=1)
        self.rewsYield = self.dataFrame[self.getFilter()][self.rewsPower].sum() * self.timeStampHours
        self.rewsYieldCount = self.dataFrame[self.getFilter()][self.rewsPower].count()
        self.rewsDelta = self.rewsYield / self.baseYield - 1.0
        self.status.addMessage("REWS Delta: %.3f%% (%d)" % (self.rewsDelta * 100.0, self.rewsYieldCount))

    def calculateTurbRenorm(self):
        self.dataFrame[self.turbulencePower] = self.dataFrame.apply(TurbulencePowerCalculator(self.powerCurve, self.ratedPower, self.inputHubWindSpeed, self.hubTurbulence).power, axis=1)
        self.turbulenceYield = self.dataFrame[self.getFilter()][self.turbulencePower].sum() * self.timeStampHours
        self.turbulenceYieldCount = self.dataFrame[self.getFilter()][self.turbulencePower].count()
        self.turbulenceDelta = self.turbulenceYield / self.baseYield - 1.0
        if self.hasActualPower:
            self.dataFrame[self.measuredTurbulencePower] = (self.dataFrame[self.actualPower] - self.dataFrame[self.turbulencePower] + self.dataFrame[self.basePower]).astype('float')
        self.status.addMessage("Turb Delta: %.3f%% (%d)" % (self.turbulenceDelta * 100.0, self.turbulenceYieldCount))

    def calculationCombined(self):
        self.dataFrame[self.combinedPower] = self.dataFrame.apply(TurbulencePowerCalculator(self.powerCurve, self.ratedPower, self.rotorEquivalentWindSpeed, self.hubTurbulence).power, axis=1)
        self.combinedYield = self.dataFrame[self.getFilter()][self.combinedPower].sum() * self.timeStampHours
        self.combinedYieldCount = self.dataFrame[self.getFilter()][self.combinedPower].count()
        self.combinedDelta = self.combinedYield / self.baseYield - 1.0
        self.status.addMessage("Comb Delta: %.3f%% (%d)" % (self.combinedDelta * 100.0, self.combinedYieldCount))

    def calculatePowerDeviationMatrixCorrection(self):

        parameterColumns = {}

        for dimension in self.specifiedPowerDeviationMatrix.dimensions:
            if dimension.parameter.lower() == "turbulence":
                parameterColumns[dimension.parameter] = self.hubTurbulence
            elif dimension.parameter.lower() == "normalisedwindspeed":
                parameterColumns[dimension.parameter] = self.normalisedWS
            elif dimension.parameter.lower() == "shearexponent":
                parameterColumns[dimension.parameter] = self.shearExponent
            else:
                raise Exception("Unknown parameter %s" % dimension.parameter)

        self.dataFrame[self.powerDeviationMatrixPower] = self.dataFrame.apply(PowerDeviationMatrixPowerCalculator(self.powerCurve, \
                                                                                                                  self.specifiedPowerDeviationMatrix, \
                                                                                                                  self.inputHubWindSpeed, \
                                                                                                                  parameterColumns).power, \
                                                                                                                  axis=1)

        self.powerDeviationMatrixYield = self.dataFrame[self.getFilter()][self.powerDeviationMatrixPower].sum() * self.timeStampHours
        self.powerDeviationMatrixYieldCount = self.dataFrame[self.getFilter()][self.powerDeviationMatrixPower].count()
        self.powerDeviationMatrixDelta = self.powerDeviationMatrixYield / self.baseYield - 1.0
        self.status.addMessage("Power Deviation Matrix Delta: %f%% (%d)" % (self.powerDeviationMatrixDelta * 100.0, self.powerDeviationMatrixYieldCount))

    def export(self, path,clean = True,  full = True, calibration = True ):
        op_path = os.path.dirname(path)
        plotsDir = self.config.path.replace(".xml","_PPAnalysisPlots")
        self.png_plots(plotsDir)
        if clean:
            self.dataFrame.to_csv(path, sep = '\t')
        if full:
            rootPath = self.config.path.split(".")[0] + "_TimeSeriesData"
            chckMake(rootPath)
            for ds in self.datasetConfigs:
                ds.data.fullDataFrame.to_csv(rootPath + os.sep + "FilteredDataSet_AllColumns_{0}.dat".format(ds.name), sep = '\t')
                if calibration and hasattr(ds.data,"filteredCalibrationDataframe"):
                    ds.data.filteredCalibrationDataframe.to_csv(rootPath + os.sep + "CalibrationDataSet_{0}.dat".format(ds.name), sep = '\t')

    def png_plots(self,path):
        chckMake(path)
        from plots import MatplotlibPlotter
        plotter = MatplotlibPlotter(path,self)
        if self.hasActualPower:
            plotter.plotPowerCurve(self.inputHubWindSpeed, self.actualPower, self.allMeasuredPowerCurve, specified_title = 'Warranted', mean_title = 'Measured Mean', gridLines = True)
            plotter.plotPowerCurve(self.inputHubWindSpeed, self.actualPower, self.allMeasuredPowerCurve, show_scatter = False, fname = "PowerCurve - Warranted vs Measured Mean", specified_title = 'Warranted', mean_title = 'Measured Mean', mean_pc_color = 'blue', gridLines = True)
            if self.turbRenormActive:
                plotter.plotTurbCorrectedPowerCurve(self.inputHubWindSpeed, self.measuredTurbulencePower, self.allMeasuredTurbCorrectedPowerCurve)
            if self.hasAllPowers:
                plotter.plotPowerLimits(specified_title = 'Warranted', gridLines = True)
        plotter.plotBy(self.windDirection,self.hubWindSpeed,self.dataFrame, gridLines = True)
        plotter.plotBy(self.windDirection,self.shearExponent,self.dataFrame, gridLines = True)
        plotter.plotBy(self.windDirection,self.hubTurbulence,self.dataFrame, gridLines = True)
        plotter.plotBy(self.hubWindSpeed,self.hubTurbulence,self.dataFrame, gridLines = True)
        if self.hasActualPower:
            plotter.plotBy(self.hubWindSpeed,self.powerCoeff,self.dataFrame, gridLines = True)
            plotter.plotBy('Input Hub Wind Speed',self.powerCoeff,self.allMeasuredPowerCurve, gridLines = True)
        if self.inflowAngle in self.dataFrame.columns:
            self.dataFrame.loc[self.dataFrame[self.inflowAngle]>180,self.inflowAngle] -= 360
            plotter.plotBy(self.windDirection,self.inflowAngle,self.dataFrame, gridLines = True)
        plotter.plotCalibrationSectors()
        if self.hasActualPower:
            if len(self.powerCurveSensitivityResults.keys()) > 0:
                for sensCol in self.powerCurveSensitivityResults.keys():
                    plotter.plotPowerCurveSensitivity(sensCol)
                plotter.plotPowerCurveSensitivityVariationMetrics()
            if len(self.dataFrame[self.nameColumn].unique()) > 1:
                plotter.plot_multiple(self.inputHubWindSpeed, self.actualPower, self.allMeasuredPowerCurve)

class PadderFactory:
    @staticmethod
    def generate(strPadder, powerCol, wsCol, turbCol, countCol):

        strPadder = strPadder.lower()
        
        if strPadder  == 'none':
            return NonePadder(powerCol, wsCol, turbCol, countCol)
        elif strPadder  == 'observed':
            return LastObservedPadder(powerCol, wsCol, turbCol, countCol)
        elif strPadder  == 'max':
            return MaxPadder(powerCol, wsCol, turbCol, countCol)
        else:
            raise Exception("Power curve padding option not detected/recognised: %s" % strPadder)

class Padder:

    def __init__(self, powerCol, wsCol, turbCol, countCol):

        self.powerCol = powerCol
        self.wsCol = wsCol
        self.turbCol = turbCol
        self.countCol = countCol
        
    def getWindSpeedBins(self, bins):

        binArray = []

        for i in range(bins.numberOfBins):
            binArray.append(bins.binCenterByIndex(i))

        return binArray

    def levelExists(self, powerLevels, windSpeed):
        
        try:
            dummy = powerLevels.loc[windSpeed, self.powerCol]
            return True
        except:
            return False
            
    def turbulencePadValue(self, powerLevels, windSpeed):

        #revisit this logic
        
        if windSpeed > self.max_key:
            return powerLevels.loc[self.max_key, self.turbCol]
        elif windSpeed < self.min_key:
            return powerLevels.loc[self.min_key, self.turbCol]
        else:
            return powerLevels.loc[self.max_key, self.turbCol]
        
    def pad(self, powerLevels, cutInWindSpeed, cutOutWindSpeed, ratedPower, bins):

        self.min_key = min(powerLevels.index)
        self.max_key = max(powerLevels.index)
        
        for windSpeed in self.getWindSpeedBins(bins):
            
            if not self.levelExists(powerLevels, windSpeed):

                powerPadValue = self.powerPadValue(powerLevels, windSpeed)
                turbulencePadValue = self.turbulencePadValue(powerLevels, windSpeed)

                if windSpeed > cutOutWindSpeed:
                    powerLevels.loc[windSpeed, self.powerCol] = 0.0
                else:

                    if windSpeed < self.min_key:
                        powerLevels.loc[windSpeed, self.powerCol] = 0.0
                    else:
                        powerLevels.loc[windSpeed, self.powerCol] = powerPadValue
                    
                powerLevels.loc[windSpeed, self.turbCol] = turbulencePadValue
                powerLevels.loc[windSpeed, self.wsCol] = windSpeed
                powerLevels.loc[windSpeed, self.countCol] = 0
            
        powerLevels.sort_index(inplace=True)
        
        return powerLevels

class NonePadder(Padder):

    def pad(self, powerLevels, cutInWindSpeed, cutOutWindSpeed, ratedPower, bins):
        return powerLevels
    
class MaxPadder(Padder):

    def powerPadValue(self, powerLevels, windSpeed):
        return powerLevels[self.powerCol].max()
  
class LastObservedPadder(Padder):

    def powerPadValue(self, powerLevels, windSpeed):
        return powerLevels[self.max_key, self.powerCol]
