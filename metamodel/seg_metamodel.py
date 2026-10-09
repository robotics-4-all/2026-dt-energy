"""Ecore metamodel aligned 1:1 with grammar/smartenergygrid.tx.
Derived from modelss/final_metamodel.py; every change is marked  # CHANGED / # NEW.
Running this file writes seg_metamodel.ecore."""
import os
from pyecore.resources import ResourceSet, URI
from pyecore.ecore import (EPackage, EClass, EAttribute, EString, EFloat, EEnum,
                           EEnumLiteral, EReference, EBoolean, EInt)

pkg = EPackage('smartgrid', nsURI='http://generic.smartgrid/core', nsPrefix='sg')

def enum(name, literals):
    e = EEnum(name)
    for i, l in enumerate(literals):
        e.eLiterals.append(EEnumLiteral(l, value=i))
    return e

# CHANGED: literals now equal the DSL rule OperationalStatus
StatusEnum = enum('OperationalStatus', ['ACTIVE', 'INACTIVE', 'MAINTENANCE', 'FAULT', 'STANDBY'])
# NEW: enums for the distribution blocks
DistVariable = enum('DistributionVariable', ['wind_speed', 'solar_noise', 'load_noise', 'voltage_noise',
                                             'frequency_noise', 'soc_noise', 'congestion_noise'])
NodeClassName = enum('NodeClassName', ['ThermalPlant', 'SolarPark', 'WindFarm', 'Residential', 'Industrial',
                                       'SubStation', 'BatteryStorage', 'Transformer'])

SpatialData = EClass('SpatialData')
SpatialData.eStructuralFeatures.extend([EAttribute('locationName', EString),
                                        EAttribute('latitude', EFloat), EAttribute('longitude', EFloat)])

# NEW: distribution classes (DSL: Distribution, DistributionEntry, Global/ClassDistributions)
Distribution = EClass('Distribution', abstract=True)
def dist(name, feats):
    c = EClass(name, superclass=Distribution)
    c.eStructuralFeatures.extend([EAttribute(f, EFloat) for f in feats])
    return c
WeibullDist = dist('WeibullDist', ['shape', 'scale'])
NormalDist = dist('NormalDist', ['mu', 'sigma'])
BetaDist = dist('BetaDist', ['alpha', 'beta'])
LogNormalDist = dist('LogNormalDist', ['mu', 'sigma'])
GammaDist = dist('GammaDist', ['shape', 'scale'])
PoissonDist = dist('PoissonDist', ['lam'])

DistributionEntry = EClass('DistributionEntry')
DistributionEntry.eStructuralFeatures.extend([EAttribute('variable', DistVariable, lower=1),
                                              EReference('distribution', Distribution, containment=True, lower=1)])
GlobalDistributions = EClass('GlobalDistributions')
GlobalDistributions.eStructuralFeatures.extend([EAttribute('symbol', EString),
                                                EReference('distributions', DistributionEntry, containment=True, upper=-1)])
ClassDistributions = EClass('ClassDistributions')
ClassDistributions.eStructuralFeatures.extend([EAttribute('symbol', EString),
                                               EAttribute('appliesTo', NodeClassName, lower=1),
                                               EReference('distributions', DistributionEntry, containment=True, upper=-1)])

GridElement = EClass('GridElement', abstract=True)
PowerLine = EClass('PowerLine')
PowerLine.eStructuralFeatures.extend([
    EAttribute('symbol', EString),            # NEW: DSL name=ID
    EAttribute('id', EString),                # DSL lineId
    EAttribute('lengthKM', EFloat), EAttribute('maxCapacityMW', EFloat),
    EReference('source', GridElement, lower=1), EReference('target', GridElement, lower=1)])

GridElement.eStructuralFeatures.extend([
    EAttribute('symbol', EString),            # NEW: DSL name=ID (target of references)
    EAttribute('id', EString),                # DSL elementId
    EAttribute('name', EString),              # DSL elementName
    EAttribute('voltageLevel', EFloat), EAttribute('status', StatusEnum),
    EReference('spatialData', SpatialData, containment=True),
    EReference('lines', PowerLine, containment=True, upper=-1),
    EReference('distributions', DistributionEntry, containment=True, upper=-1)])  # NEW: per-node overrides

EnergyNode = EClass('EnergyNode', superclass=GridElement, abstract=True)
EnergyNode.eStructuralFeatures.extend([EAttribute(n, EFloat) for n in
    ['measuredVoltage', 'measuredCurrent', 'powerFactor', 'frequency', 'activePower', 'reactivePower']] +
    [EAttribute('lastUpdate', EString), EAttribute('locationRef', EString), EAttribute('isControllable', EBoolean)])

Producer = EClass('Producer', superclass=EnergyNode, abstract=True)
Producer.eStructuralFeatures.extend([EAttribute('maxCapacityMW', EFloat), EAttribute('currentOutputMW', EFloat)])
ThermalPlant = EClass('ThermalPlant', superclass=Producer)
ThermalPlant.eStructuralFeatures.extend([EAttribute('fuelType', EString), EAttribute('efficiency', EFloat),
                                         EAttribute('thermalOutputMW', EFloat), EAttribute('co2SpecificEmissions', EFloat)])
SolarPark = EClass('SolarPark', superclass=Producer)
SolarPark.eStructuralFeatures.extend([EAttribute('panelType', EString), EAttribute('cloudCover', EFloat)])
WindFarm = EClass('WindFarm', superclass=Producer)
WindFarm.eStructuralFeatures.extend([EAttribute('windSpeed', EFloat), EAttribute('turbineCount', EInt)])  # CHANGED: EFloat->EInt (DSL INT)

Consumer = EClass('Consumer', superclass=EnergyNode, abstract=True)
Consumer.eStructuralFeatures.extend([EAttribute('demandMW', EFloat), EAttribute('currentConsumption', EFloat),
                                     EAttribute('dailyProfile', EString)])  # NEW: 24 comma-separated hourly factors
Residential = EClass('Residential', superclass=Consumer)
Residential.eStructuralFeatures.extend([EAttribute('numberOfResidents', EInt), EAttribute('hasSmartAppliances', EBoolean)])
Industrial = EClass('Industrial', superclass=Consumer)
Industrial.eStructuralFeatures.extend([EAttribute('industryType', EString)])

SubStation = EClass('SubStation', superclass=GridElement)
SubStation.eStructuralFeatures.extend([EAttribute('congestionLevel', EFloat), EReference('connectedTo', SubStation, upper=-1)])
BatteryStorage = EClass('BatteryStorage', superclass=GridElement)
BatteryStorage.eStructuralFeatures.extend([EAttribute('capacityMWh', EFloat), EAttribute('stateOfCharge', EFloat)])
Transformer = EClass('Transformer', superclass=GridElement)
Transformer.eStructuralFeatures.extend([EAttribute(n, EFloat) for n in
                                        ['ratingKVA', 'efficiency', 'primaryVoltage', 'secondaryVoltage']])

PowerGrid = EClass('PowerGrid')   # CHANGED: no 'elements' (the DSL grid does not own elements)
PowerGrid.eStructuralFeatures.extend([EAttribute('symbol', EString), EAttribute('gridName', EString),
                                      EAttribute('region', EString)])

# NEW: root = DSL rule SmartEnergyGridModel
Model = EClass('SmartEnergyGridModel')
Model.eStructuralFeatures.extend([
    EReference('grids', PowerGrid, containment=True, upper=-1),
    EReference('elements', GridElement, containment=True, upper=-1),
    EReference('globalDistributions', GlobalDistributions, containment=True, upper=-1),
    EReference('classDistributions', ClassDistributions, containment=True, upper=-1)])

pkg.eClassifiers.extend([StatusEnum, DistVariable, NodeClassName, SpatialData, Distribution, WeibullDist, NormalDist,
    BetaDist, LogNormalDist, GammaDist, PoissonDist, DistributionEntry, GlobalDistributions, ClassDistributions,
    GridElement, EnergyNode, Producer, ThermalPlant, SolarPark, WindFarm, Consumer, Residential, Industrial,
    SubStation, BatteryStorage, Transformer, PowerLine, PowerGrid, Model])

if __name__ == '__main__':
    rs = ResourceSet(); r = rs.create_resource(URI(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seg_metamodel.ecore'))); r.append(pkg); r.save()
    print('seg_metamodel.ecore written')
