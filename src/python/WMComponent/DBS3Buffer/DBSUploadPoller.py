#!/usr/bin/env python

"""
The DBSUpload algorithm

This code, all of which is in uploadDatasets works in three parts.
Each part is carried out in its own database transaction.

1) Find all files that have not yet been uploaded to DBS,
   and the fileset-algo pairs that go along with them.
2) Upload these files.  This is handled through DBSInterface
   which first sends the algo, then the dataset, and then
   commits all of the files.  It also handles migrating to
   global is you specify a global URL and the file blocks are
   full (have reached the limit in the DBS config)
3) The code cleans up at the end, check for any open blocks
   that have exceeded their timeout.


NOTE: This is complicated as hell, because you can have a
dataset-algo pair where BOTH the dataset and the algo are in
DBS attached to different objects.  You have to scan for a pair
that is not in DBS, decide whether its components are in DBS,
add them, and then add the files.  This is why everything is
so convoluted.
"""

import time
import threading
import logging
import Queue
import traceback
import multiprocessing

from WMCore.WorkerThreads.BaseWorkerThread import BaseWorkerThread
from WMCore.Services.UUID                  import makeUUID
from WMCore.WMException                    import WMException


from WMComponent.DBS3Buffer.DBSBufferUtil  import DBSBufferUtil
from WMComponent.DBS3Buffer.DBSBufferBlock import DBSBlock

from dbs.apis.dbsClient import DbsApi



def createConfigForJSON(config):
    """
    Turn a config object into a dictionary of dictionaries

    """

    final = {}
    for sectionName in config.listSections_():
        section = getattr(config, sectionName)
        if hasattr(section, 'dictionary_'):
            # Create a dictionary key for it
            final[sectionName] = createDictionaryFromConfig(section)


    return final


def createDictionaryFromConfig(configSection):
    """
    Recursively create dictionaries from config

    """


    final = configSection.dictionary_()

    for key in final.keys():
        if hasattr(final[key], 'dictionary_'):
            # Then we can turn it into a dictionary
            final[key] = createDictionaryFromConfig(final[key])

    return final



def sortListByKey(input, key):
    """
    Return list of dictionaries as a
    dictionary of lists of dictionaries
    keyed by one original key

    """
    final = {}

    for entry in input:
        value = entry.get(key)
        if type(value) == set:
            value = value.pop()
        if not value in final.keys():
            final[value] = []
        final[value].append(entry)

    return final

def uploadWorker(input, results, dbsUrl):
    """
    _uploadWorker_

    Put JSONized blocks in the input
    Get confirmation in the output
    """

    # Init DBS Stuff
    logging.debug("Creating dbsAPI with address %s" % dbsUrl)
    dbsApi = DbsApi(url = dbsUrl)


    while True:

        try:
            work = input.get()
        except (EOFError, IOError):
            crashMessage = "Hit EOF/IO in getting new work\n"
            crashMessage += "Assuming this is a graceful break attempt.\n"
            logging.error(crashMessage)
            break

        if work == 'STOP':
            # Then halt the process
            break

        name  = work.get('name', None)
        block = work.get('block', None)

        # Do stuff with DBS
        try:
            logging.debug("About to call insert block with block: %s" % block)
            dbsApi.insertBulkBlock(blockDump = block)
            results.put({'name': name, 'success': "uploaded"})
        except Exception as ex:
            exString = str(ex)
            if 'Block %s already exists' % name in exString:
                # Then this is probably a duplicate
                # Ignore this for now
                logging.error("Had duplicate entry for block %s. Ignoring for now." % name)
                logging.debug("Exception: %s" % exString)
                logging.debug("Traceback: %s" % str(traceback.format_exc()))
                results.put({'name': name, 'success': "uploaded"})
            elif 'Proxy Error' in exString:
                # This is probably a successfully inserton that went bad.
                # Put it on the check list
                msg = "Got a proxy error for block (%s)." % name
                logging.error(msg)
                logging.error(str(traceback.format_exc()))
                results.put({'name': name, 'success': "check"})
            else:
                msg =  "Error trying to process block %s through DBS.\n" % name
                msg += exString
                logging.error(msg)
                logging.error(str(traceback.format_exc()))
                logging.debug("block: %s \n" % block)
                results.put({'name': name, 'success': "error", 'error': msg})

    return

class DBSUploadException(WMException):
    """
    Holds the exception info for
    all the things that will go wrong

    """

class DBSUploadPoller(BaseWorkerThread):
    """
    Handles poll-based DBSUpload

    """


    def __init__(self, config, dbsconfig = None):
        """
        Initialise class members
        """
        logging.info("Running __init__ for DBS3 Uploader")
        BaseWorkerThread.__init__(self)
        self.config     = config

        # This is slightly dangerous, but DBSUpload depends
        # on DBSInterface anyway
        self.dbsUrl           = self.config.DBS3Upload.dbsUrl

        self.dbsUtil = DBSBufferUtil()


        self.pool   = []
        self.blocksToCheck = []
        self.input  = None
        self.result = None
        self.nProc  = getattr(self.config.DBS3Upload, 'nProcesses', 4)
        self.wait   = getattr(self.config.DBS3Upload, 'dbsWaitTime', 2)
        self.nTries = getattr(self.config.DBS3Upload, 'dbsNTries', 300)
        self.dbs3UploadOnly = getattr(self.config.DBS3Upload, "dbs3UploadOnly", False)
        self.physicsGroup   = getattr(self.config.DBS3Upload, "physicsGroup", "NoGroup")
        self.datasetType    = getattr(self.config.DBS3Upload, "datasetType", "PRODUCTION")
        self.primaryDatasetType = getattr(self.config.DBS3Upload, "primaryDatasetType", "mc")
        self.blockCount     = 0
        self.dbsApi = DbsApi(url = self.dbsUrl)

        # List of blocks currently in processing
        self.queuedBlocks = []

        # Set up the pool of worker processes
        self.setupPool()

        # Setting up any cache objects
        self.blockCache = {}
        self.dasCache   = {}

        self.filesToUpdate = []

        self.produceCopy = getattr(self.config.DBS3Upload, 'copyBlock', False)
        self.copyPath    = getattr(self.config.DBS3Upload, 'copyBlockPath',
                                   '/data/mnorman/block.json')
        
        self.timeoutWaiver = 1

        return

    def setupPool(self):
        """
        _setupPool_

        Set up the processing pool for work
        """
        if len(self.pool) > 0:
            # Then something already exists.  Continue
            return

        self.input  = multiprocessing.Queue()
        self.result = multiprocessing.Queue()

        # Starting up the pool:
        for _ in range(self.nProc):
            p = multiprocessing.Process(target = uploadWorker,
                                        args = (self.input,
                                                self.result, self.dbsUrl))
            p.start()
            self.pool.append(p)

        return

    def __del__(self):
        """
        __del__

        Trigger a close of connections if necessary
        """
        self.close()
        return

    def close(self):
        """
        _close_

        Kill all connections and terminate
        """
        terminate = False
        for _ in self.pool:
            try:
                self.input.put('STOP')
            except Exception as ex:
                # Something very strange happens here
                # It's like it raises a blank exception
                # Upon being told to return
                msg =  "Hit some exception in deletion\n"
                msg += str(ex)
                logging.debug(msg)
                terminate = True
        try:
            self.input.close()
            self.result.close()
        except:
            # What are you going to do?
            pass
        for proc in self.pool:
            if terminate:
                proc.terminate()
            else:
                proc.join()
        self.pool   = []
        self.input  = None
        self.result = None
        return



    def terminate(self, params):
        """
        Do one more pass, then terminate

        """
        logging.debug("terminating. doing one more pass before we die")
        self.algorithm(params)


    def algorithm(self, parameters = None):
        """
        _algorithm_

        First, check blocks that may be already uploaded
        Then, load blocks
        Then, load files
        Then, move files into blocks
        Then add new blocks in DBSBuffer
        Then add blocks to DBS
        Then mark blocks as done in DBSBuffer
        """
        try:
            logging.info("Starting the DBSUpload Polling Cycle")
            self.checkBlocks()
            self.loadBlocks()

            # The following two functions will actually place new files into
            # blocks.  In DBS3 upload mode we rely on something else to do that
            # for us and will skip this step.
            if not self.dbs3UploadOnly:
                self.loadFiles()
                self.checkTimeout()
                self.checkCompleted()

            self.inputBlocks() 
            self.retrieveBlocks()
        except WMException:
            raise
        except Exception as ex:
            msg =  "Unhandled Exception in DBSUploadPoller!\n"
            msg += str(ex)
            msg += str(str(traceback.format_exc()))
            logging.error(msg)
            raise DBSUploadException(msg)

    def loadBlocks(self):
        """
        _loadBlocks_

        Find all blocks; make sure they're in the cache
        """
        openBlocks = self.dbsUtil.findOpenBlocks(self.dbs3UploadOnly)
        logging.info("These are the openblocks: %s" % openBlocks)

        # Load them if we don't have them
        blocksToLoad = []
        for block in openBlocks:
            if not block['blockname'] in self.blockCache.keys():
                blocksToLoad.append(block['blockname'])


        # Now load the blocks
        try:
            loadedBlocks = self.dbsUtil.loadBlocks(blocksToLoad, self.dbs3UploadOnly)
            logging.info("Loaded blocks: %s" % loadedBlocks)
        except WMException:
            raise
        except Exception as ex:
            msg =  "Unhandled exception while loading blocks.\n"
            msg += str(ex)
            logging.error(msg)
            logging.debug("Blocks to load: %s\n" % blocksToLoad)
            raise DBSUploadException(msg)

        for blockInfo in loadedBlocks:
            das  = blockInfo['DatasetAlgo']
            loc  = blockInfo['origin_site_name']
            workflow =  blockInfo['workflow']
            block = DBSBlock(name = blockInfo['block_name'],
                             location = loc, das = das, workflow = workflow)
            block.FillFromDBSBuffer(blockInfo)
            blockname = block.getName()

            # Now we have to load files...
            try:
                files = self.dbsUtil.loadFilesByBlock(blockname = blockname)
                logging.info("Have %i files for block %s" % (len(files), blockname))
            except WMException:
                raise
            except Exception as ex:
                msg =  "Unhandled exception while loading files for existing blocks.\n"
                msg += str(ex)
                logging.error(msg)
                logging.debug("Blocks being loaded: %s\n" % blockname)
                raise DBSUploadException(msg)

            # Add the loaded files to the block
            for file in files:
                block.addFile(file, self.datasetType, self.primaryDatasetType)

            # Add to the cache
            self.addNewBlock(block = block)

        # All blocks should now be loaded and present
        # in both the block cache (which has all the info)
        # and the dasCache (which is a list of name pointers
        # to the keys in the block cache).
        return


    def loadFiles(self):
        """
        _loadFiles_

        Load all files that need to be loaded.  I will do this by DAS for now to
        break the monstrous calls down into smaller chunks.
        """
        # Grab all the Dataset-Algo combindations
        dasList = self.dbsUtil.findUploadableDAS()

        if len(dasList) < 1:
            # Then there's nothing to do
            return []

        readyBlocks = []
        for dasInfo in dasList:

            dasID = dasInfo['DAS_ID']

            # Get the files
            try:
                loadedFiles = self.dbsUtil.findUploadableFilesByDAS(das = dasID)
            except WMException:
                raise
            except Exception as ex:
                msg =  "Unhandled exception while loading uploadable files for DAS.\n"
                msg += str(ex)
                logging.error(msg)
                logging.debug("DAS being loaded: %s\n" % dasID)
                raise DBSUploadException(msg)

            # Sort the files and blocks by location
            fileDict = sortListByKey(input = loadedFiles, key = 'locations')

            # Now add each file
            for location in fileDict.keys():
                files = fileDict.get(location)

                if len(files) < 1:
                    # Nothing to do here
                    continue

                currentBlock = self.getBlock(files[0], location, dasID, True)
                currentBlock.setAcquisitionEra(era = dasInfo['AcquisitionEra'])
                currentBlock.setProcessingVer(procVer = dasInfo['ProcessingVer'])

                for newFile in files:
                    if not newFile.get('block', 1) == None:
                        # Then this file already has a block
                        # It should be accounted for somewhere
                        # Or loaded with the block
                        continue

                    # Check if we can put files in this block
                    if not self.isBlockOpen(newFile = newFile,
                                            block = currentBlock):
                        # Then we have to close the block and get a new one
                        currentBlock.setPendingAndCloseBlock()
                        readyBlocks.append(currentBlock)
                        currentBlock = self.getBlock(newFile = newFile,
                                                     location = location,
                                                     das = dasID)
                        currentBlock.setAcquisitionEra(era = dasInfo['AcquisitionEra'])
                        currentBlock.setProcessingVer(procVer = dasInfo['ProcessingVer'])

                    # Now deal with the file
                    currentBlock.addFile(newFile, self.datasetType, self.primaryDatasetType)
                    self.filesToUpdate.append({'filelfn': newFile['lfn'],
                                               'block': currentBlock.getName()})
                # Done with the location
                readyBlocks.append(currentBlock)

            # Should be done with the DAS once we've added all files

        # Update the blockCache with what is now ready.
        for block in readyBlocks:
            self.blockCache[block.getName()] = block
        return

    def checkTimeout(self):
        """
        _checkTimeout_

        Loop all Open blocks and mark them as Pending if they have timed out.
        """
        for block in self.blockCache.values():
            if block.status == "Open" and block.getTime() > block.getMaxBlockTime():
                block.setPendingAndCloseBlock()
                self.blockCache[block.getName()] = block
    
    def checkCompleted(self):
        """
        _checkTimeout_

        Loop all Open blocks and mark them as Pending if they have timed out.
        """
        completedWorkflows = self.dbsUtil.getCompletedWorkflows()
        for block in self.blockCache.values():
            if block.status == "Open":
                if block.workflow in completedWorkflows:
                    block.setPendingAndCloseBlock()
                    self.blockCache[block.getName()] = block

    def addNewBlock(self, block):
        """
        _addNewBlock_

        Add a new block everywhere it has to go
        """
        name     = block.getName()
        location = block.getLocation()
        das      = block.das

        self.blockCache[name] = block
        if not das in self.dasCache.keys():
            self.dasCache[das] = {}
            self.dasCache[das][location] = []
        elif not location in self.dasCache[das].keys():
            self.dasCache[das][location] = []
        if name not in self.dasCache[das][location]:
            self.dasCache[das][location].append(name)

        return

    def isBlockOpen(self, newFile, block, doTime = False):
        """
        _isBlockOpen_

        Check and see if a block is full
        This will check on time, but that's disabled by default
        The plan is to do a time check after we do everything else,
        so open blocks about to time out can still get more
        files put in them.
        """

        if block.getMaxBlockFiles() is None or block.getMaxBlockNumEvents() is None or \
            block.getMaxBlockSize() is None or block.getMaxBlockTime() is None:
            return True
        if block.status != 'Open':
            # Then somebody has dumped this already
            return False
        if block.getSize() + newFile['size'] > block.getMaxBlockSize():
            return False
        if block.getNumEvents() + newFile['events'] > block.getMaxBlockNumEvents():
            return False
        if block.getNFiles() >= block.getMaxBlockFiles():
            # Then we have to dump it because this file
            # will put it over the limit.
            return False
        if block.getTime() > block.getMaxBlockTime() and doTime:
            return False

        return True

    def getBlock(self, newFile, location, das, skipOpenCheck = False):
        """
        _getBlock_

        Retrieve a block is one exists and is open.  If no open block is found
        create and return a new one.
        """
        if das in self.dasCache.keys() and location in self.dasCache[das].keys():
            for blockName in self.dasCache[das][location]:
                block = self.blockCache.get(blockName)
                if not self.isBlockOpen(newFile = newFile, block = block) and not skipOpenCheck:
                    # Block isn't open anymore.  Mark it as pending so that it gets
                    # uploaded.
                    block.setPendingAndCloseBlock()
                    self.blockCache[blockName] = block
                else:
                    return block

        # A suitable open block does not exist.  Create a new one.
        blockname = "%s#%s" % (newFile["datasetPath"], makeUUID())
        newBlock = DBSBlock(name = blockname, location = location, 
                            das = das, workflow = newFile["workflow"])
        self.addNewBlock(block = newBlock)
        return newBlock

    def inputBlocks(self):
        """
        _inputBlocks_

        Loop through all of the "active" blocks and sort them so we can act
        appropriately on them.  Everything will be sorted based on the
        following:
         Queued - Block is already being acted on by another process.  We just
          ignore it.
         Pending, not in DBSBuffer - Block that has been closed and needs to
           be injected into DBS and also written to DBSBuffer.  We'll do both.
         Pending, in DBSBuffer - Block has been closed and written to
           DBSBuffer.  We just need to inject it into DBS.
         Open, not in DBSBuffer - Newly created block that needs to be written
           not DBSBuffer.
         Open, in DBSBuffer - Newly created block that has already been
           written to DBSBuffer.  We don't have to do anything with it.
        """
        myThread = threading.currentThread()

        createInDBS = []
        createInDBSBuffer = []
        updateInDBSBuffer = []
        for block in self.blockCache.values():
            if block.getName() in self.queuedBlocks:
                # Block is already being dealt with by another process.  We'll
                # ignore it here.
                continue
            if block.status == 'Pending':
                # All pending blocks need to be injected into DBS.
                createInDBS.append(block)

                # If this is a new block it needs to be added to DBSBuffer
                # otherwise it just needs to be updated in DBSBuffer.
                if not block.inBuff:
                    createInDBSBuffer.append(block)
                else:
                    updateInDBSBuffer.append(block)
            if block.status == 'Open' and not block.inBuff:
                # New block that needs to be added to DBSBuffer.
                createInDBSBuffer.append(block)

        # Build the pool if it was closed
        if len(self.pool) == 0:
            self.setupPool()

        # First handle new and updated blocks
        try:
            myThread.transaction.begin()
            self.dbsUtil.createBlocks(blocks = createInDBSBuffer)
            self.dbsUtil.updateBlocks(blocks = updateInDBSBuffer,
                                      dbs3UploadOnly = self.dbs3UploadOnly)
            myThread.transaction.commit()
        except WMException:
            myThread.transaction.rollback()
            raise
        except Exception as ex:
            msg =  "Unhandled exception while writing new blocks into DBSBuffer\n"
            msg += str(ex)
            logging.error(msg)
            logging.debug("Blocks for DBSBuffer: %s\n" % createInDBSBuffer)
            logging.debug("Blocks for Update: %s\n" % updateInDBSBuffer)
            myThread.transaction.rollback()
            raise DBSUploadException(msg)

        # Update block status in the block cache.  Mark the blocks that we have
        # added to DBSBuffer as being in DBSBuffer.
        for block in createInDBSBuffer:
            self.blockCache.get(block.getName()).inBuff = True

        # Record new file/block associations in DBSBuffer.
        try:
            myThread.transaction.begin()
            self.dbsUtil.setBlockFiles(binds = self.filesToUpdate)
            self.filesToUpdate = []
            myThread.transaction.commit()
        except WMException:
            myThread.transaction.rollback()
            raise
        except Exception as ex:
            msg =  "Unhandled exception while setting blocks in files.\n"
            msg += str(ex)
            logging.error(msg)
            logging.debug("Files to Update: %s\n" % self.filesToUpdate)
            myThread.transaction.rollback()
            raise DBSUploadException(msg)

        # Finally upload blocks to DBS.
        for block in createInDBS:
            if len(block.files) < 1:
                # What are we doing?
                logging.debug("Skipping empty block")
                continue
            if block.getDataset() == None:
                # Then we have to fix the dataset
                dbsFile = block.files[0]
                block.setDataset(datasetName  = dbsFile['datasetPath'],
                                 primaryType  = self.primaryDatasetType,
                                 datasetType  = self.datasetType,
                                 physicsGroup = dbsFile.get('physicsGroup', None),
                                 prep_id = dbsFile.get('prep_id', None))
            logging.debug("Found block %s in blocks" % block.getName())
            block.setPhysicsGroup(group = self.physicsGroup)
            
            encodedBlock = block.convertToDBSBlock()
            logging.info("About to insert block %s" % block.getName())
            self.input.put({'name': block.getName(), 'block': encodedBlock})
            self.blockCount += 1
            if self.produceCopy:
                import json
                f = open(self.copyPath, 'w')
                f.write(json.dumps(encodedBlock))
                f.close()
            self.queuedBlocks.append(block.getName())

        # And all work is in and we're done for now
        return

    def retrieveBlocks(self):
        """
        _retrieveBlocks_

        Once blocks are in DBS, we have to retrieve them and see what's
        in them.  What we do is get everything out of the result queue,
        and then update it in DBSBuffer.

        To do this, the result queue needs to pass back the blockname
        """
        myThread = threading.currentThread()

        blocksToClose = []
        emptyCount    = 0
        while self.blockCount > 0:
            if emptyCount > self.nTries:
                
                # When timeoutWaiver is 0 raise error. 
                # It could take long time to get upload data to DBS 
                # if there are a lot of files are cumulated in the buffer.
                # in first try but second try should be faster.
                # timeoutWaiver is set as component variable - only resets when component restarted.
                # The reason for that is only back log will occur when component is down 
                # for a long time while other component still running and feeding the data to 
                # dbsbuffer
        
                if self.timeoutWaiver == 0:
                    msg = "Exceeded max number of waits while waiting for DBS to finish"
                    raise DBSUploadException(msg)
                else:
                    self.timeoutWaiver = 0
                    return
            try:
                # Get stuff out of the queue with a ridiculously
                # short wait time
                blockresult = self.result.get(timeout = self.wait)
                blocksToClose.append(blockresult)
                self.blockCount -= 1
                logging.debug("Got a block to close")
            except Queue.Empty:
                # This means the queue has no current results
                time.sleep(2)
                emptyCount += 1
                continue

        loadedBlocks = []
        for result in blocksToClose:
            # Remove from list of work being processed
            self.queuedBlocks.remove(result.get('name'))
            if result["success"] == "uploaded":
                block = self.blockCache.get(result.get('name'))
                block.status = 'InDBS'
                loadedBlocks.append(block)
            elif result["success"] == "check":
                block = result["name"]
                self.blocksToCheck.append(block)
            else:
                logging.error("Error found in multiprocess during process of block %s" % result.get('name'))
                logging.error(result['error'])
                # Continue to the next block
                # Block will remain in pending status until it is transferred

        try:
            myThread.transaction.begin()
            self.dbsUtil.updateBlocks(loadedBlocks, self.dbs3UploadOnly)
            if not self.dbs3UploadOnly:
                self.dbsUtil.updateFileStatus(loadedBlocks, "InDBS")
            myThread.transaction.commit()
        except WMException:
            myThread.transaction.rollback()
            raise
        except Exception as ex:
            msg =  "Unhandled exception while finished closed blocks in DBSBuffer\n"
            msg += str(ex)
            logging.error(msg)
            logging.debug("Blocks for Update: %s\n" % loadedBlocks)
            myThread.transaction.rollback()
            raise DBSUploadException(msg)

        for block in loadedBlocks:
            # Clean things up
            name     = block.getName()
            location = block.getLocation()
            das      = block.das
            self.dasCache[das][location].remove(name)
            del self.blockCache[name]

        # Clean up the pool so we don't have stuff waiting around
        if len(self.pool) > 0:
            self.close()

        # And we're done
        return

    def checkBlocks(self):
        """
        _checkBlocks_

        Check with DBS3 if the blocks marked as check are
        uploaded or not.
        """
        myThread = threading.currentThread()
        blocksUploaded = []

        # See if there is anything to check
        for block in self.blocksToCheck:
            logging.debug("Checking block existence: %s" % block)
            # Check in DBS if the block was really inserted
            try:
                result = self.dbsApi.listBlocks(block_name = block)
                for blockResult in result:
                    if blockResult['block_name'] == block:
                        loadedBlock = self.blockCache.get(block)
                        loadedBlock.status = 'InDBS'
                        blocksUploaded.append(loadedBlock)
                        break
            except Exception as ex:
                exString = str(ex)
                msg =  "Error trying to check block %s through DBS.\n" % block
                msg += exString
                logging.error(msg)
                logging.error(str(traceback.format_exc()))
        # Update the status of those blocks that were truly inserted
        try:
            myThread.transaction.begin()
            self.dbsUtil.updateBlocks(blocksUploaded, self.dbs3UploadOnly)
            if not self.dbs3UploadOnly:
                self.dbsUtil.updateFileStatus(blocksUploaded, "InDBS")
            myThread.transaction.commit()
        except WMException:
            myThread.transaction.rollback()
            raise
        except Exception as ex:
            msg =  "Unhandled exception while finished closed blocks in DBSBuffer\n"
            msg += str(ex)
            logging.error(msg)
            logging.debug("Blocks for Update: %s\n" % blocksUploaded)
            myThread.transaction.rollback()
            raise DBSUploadException(msg)

        for block in blocksUploaded:
            # Clean things up
            name     = block.getName()
            location = block.getLocation()
            das      = block.das
            self.dasCache[das][location].remove(name)
            del self.blockCache[name]

        # Clean the check list
        self.blocksToCheck = []

        # We're done
        return
