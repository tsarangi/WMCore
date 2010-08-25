#!/usr/bin/env python
"""
_CMSSW_

Basic Emulator for CMSSW Step

"""
from WMCore.WMSpec.Steps.Emulator import Emulator

class CMSSW(Emulator):
    """
    _CMSSW_

    Emulate the execution of a CMSSW Step

    """
    
    def execute(self):
        """
        _emulate_


        """
        step.section_("execution")
        step.execution.exitStatus = 0
        step.section_("emulation")
        step.emulation.emulatedBy = str(self.__class__.__name__)

        print "Emulating CMSSW Step"
    
    def pre(self,step):
        '''
            _emulatePre_
            
            Emulates the pre-execution step
        '''
        return self.executor.pre()

    def emulatePost(self):
        '''
            _emulatePost_
            
            Emulates the post-execution step
        '''
        return self.executor.post()