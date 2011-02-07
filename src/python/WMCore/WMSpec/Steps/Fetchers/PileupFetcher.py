"""
Given a pile up dataset, pull the information required to cache the list
of pileup files in the job sandbox for the dataset.

"""

import os
from WMCore.WMSpec.Steps.Fetchers.FetcherInterface import FetcherInterface
from WMCore.Services.DBS.DBSReader import DBSReader
import WMCore.WMSpec.WMStep as WMStep
from DBSAPI.dbsApi import DbsApi
from WMCore.Wrappers.JsonWrapper.JSONThunker import JSONThunker
from WMCore.Wrappers.JsonWrapper import JSONEncoder


class PileupFetcher(FetcherInterface):
    """
    Pull dataset block/SE : LFN list from DBS for the
    pileup datasets required by the steps in the job.

    Save these maps as files in the sandbox

    """
    
    def _queryDbsAndGetPileupConfig(self, stepHelper, dbsApi):
        """
        Method iterates over components of the pileup configuration input
        and queries DBS. Then iterates over results from DBS.
        
        There needs to be a list of files and their locations for each
        dataset name.
        First DBS query:
            dbsFileBlocks = dbsApi.listBlocks(dataset = dataset)
            -> contains list StorageElementNames
        Second DBS query:
            dbsFiles = dbsApi.listFiles(blockName = dbsFileBlock["Name"])
            -> contains list of files each block consists of
        
        the result data structure is a Python dict following dictionary:
            FileList is a list of LFNs
        
        {"pileupTypeA": {"BlockA": {"FileList": [], "StorageElementNames": []}, 
                         "BlockB": {"FileList": [], "StorageElementName": []}, ....}
                         
        this structure preserves knowledge of where particular files of data
        set are physically (list of SEs) located. DBS only lists sites which
        have all files belonging to blocks but e.g. BlockA of dataset DS1 may
        be located at site1 and BlockB only at site2 - it's possible that only
        a subset of the blocks in a dataset will be at a site.
            
        """        
        resultDict = {}
        # iterate over input pileup types (e.g. "cosmics", "minbias")
        for pileupType in stepHelper.data.pileup.listSections_():
            # the format here is: step.data.pileup.cosmics.dataset = [/some/data/set]
            datasets = getattr(getattr(stepHelper.data.pileup, pileupType), "dataset")
            # each dataset input can generally be a list, iterate over dataset names
            blockDict = {}
            for dataset in datasets:
                dbsFileBlocks = dbsApi.listBlocks(dataset = dataset)
                # DBS listBlocks returns list of DbsFileBlock objects for each dataset,
                # iterate over and query each block to get list of files
                fileList = [] # list of files in the block (dbsFile["LogicalFileName"])
                seNames = [] # list of StorageElementName
                for dbsFileBlock in dbsFileBlocks:
                    dbsBlockName = dbsFileBlock["Name"]
                    # each DBS block has a list under 'StorageElementList', iterate over
                    for storElem in dbsFileBlock["StorageElementList"]:
                        # this entry contains the site name, e.g.:
                        # 'StorageElementList': [{'Role': '', 'Name': 'storm-fe-cms.cr.cnaf.infn.it'}]
                        seNames.append(storElem["Name"])
                    # now get list of files in the block
                    dbsFiles = dbsApi.listFiles(blockName = dbsBlockName)
                    for dbsFile in dbsFiles:
                        fileList.append(dbsFile["LogicalFileName"])
                    blockDict[dbsBlockName] = {"FileList": fileList, "StorageElementNames": seNames}
            resultDict[pileupType] = blockDict
        return resultDict
    
    
    def _createPileupConfigFile(self, helper):
        """
        Stores pileup JSON configuration file in the working
        directory / sandbox.
        
        """
        encoder = JSONEncoder()
        thunker = JSONThunker()
        args = {}
        # this should have been set in CMSSWStepHelper along with
        # the pileup configuration
        args["url"] = helper.data.dbsUrl
        args["version"] = "DBS_2_0_9"
        args["mode"] = "GET"
        dbsApi = DbsApi(args)
        
        configDict = self._queryDbsAndGetPileupConfig(helper, dbsApi)
        
        # create JSON and save into a file
        thunked = thunker.thunk(configDict)
        json = encoder.encode(thunked)
        
        stepPath = "%s/%s" % (self.workingDirectory(), helper.name())
        os.mkdir(stepPath)
        try:
            fileName = "%s/%s" % (stepPath, "pileupconf.json") 
            f = open(fileName, 'w')
            f.write(json)
            f.close()
        except IOError:
            m = "Could not save pileup JSON configuration file: '%s'" % fileName
            raise RuntimeError(m)        
    

    def __call__(self, wmTask):
        """
        Method is called  when WorkQueue creates the sandbox for a job.
        Need to look at the pileup configuration in the spec and query dbs to
        determine the lfns for the files in the datasets and what sites they're
        located at (WQ creates the job sandbox).
        
        wmTask is instance of WMTask.WMTaskHelper
        
        """
        for step in wmTask.steps().nodeIterator():
            helper = WMStep.WMStepHelper(step)
            # returns e.g. instance of CMSSWHelper
            # doesn't seem to be necessary ... strangely (some inheritance involved?)
            # typeHelper = helper.getTypeHelper()
            if hasattr(helper.data, "pileup"):
                self._createPileupConfigFile(helper)